"""Iterative EXHAUST driver — pumps an agent through the manifest until
coverage threshold (or max iterations) is reached.

Why this exists. ``hermes -z`` (and openclaw equivalents) produce one
response per invocation. The model decides when to stop within that
turn — typically 5-20 tool calls. A 600-element manifest can't be
exhausted in one turn. The driver invokes the agent repeatedly,
each time with a prompt that names a fresh batch of unprocessed
manifest elements, so each call has a clear small target.

Default agent invocation shells out to::

    ara launch --contain --state-root <state> -- hermes -z <prompt>

with all the env/cert wiring an MCP-using contained hermes session
needs. Override via ``--agent-cmd`` or pass ``agent_invoke=`` if
calling :func:`run_exhaust` programmatically.

Stopping conditions (any one ends the loop):

- Coverage fraction reaches ``target_coverage`` (default 50%).
- ``max_iterations`` invocations completed.
- Two consecutive iterations with no new extractions
  (probably stuck — bail rather than burn tokens).
- ``pending`` list empty (every element has at least one extraction).

Coverage is read fresh from ``forge.phases.exhaust.coverage_state``
between iterations; the driver doesn't trust its own bookkeeping."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ara.forge.manifest.element import ManifestElement, read_manifest
from ara.forge.phases.exhaust import CoverageState, coverage_state, record_extraction
from ara.forge.session import ForgePhase, ForgeSession
from ara.forge import state as fstate


_DEFAULT_BATCH_PROMPT_TEMPLATE = """\
You are equipped with the blacksmith-hammer. Forge session {forge_id}
is in EXHAUST phase. The agent-loop driver is calling you on iteration
{iteration} (coverage {coverage_pct:.1f}%, {pending_total} elements
still pending out of {manifest_total}).

THIS turn's target: extract structured content from these specific
manifest elements:

{element_block}

For each manifest_element_id above:

1. Call mcp_ara_forge_mats_get with forge_id="{forge_id}" and the exact
   manifest_element_id to fetch the element's text.
2. Decide what kind of content it is:

   **Knowledge alphabets** — what we test FOR. THIS IS THE DEFAULT
   for security-research material (WSTG / OWASP / vendor reports /
   hardware toolkits / etc.):
   - extract.proc — a procedure / step pattern
   - extract.conc — a conceptual shape / template / rubric / matrix
   - extract.fact_shape — a structured assertion schema
   - extract.store — an internal fact/evidence store entry
   - extract.resource — an external reference, wordlist, script,
     package, repository, vendor_doc, vuln_db, or tool_doc the task may
     need later. If the source names a resource but not a verified URL,
     emit the resource intent with hints and no locator; the operator
     must resolve it before it grants network access.
   - extract.line — an in-scope / out-of-scope boundary

   **Agent self-discipline** — ONLY for source text describing how
   THE TESTING AGENT ITSELF should behave during the engagement
   (throttling, credential redaction, never write to operator home,
   approval gates on destructive commands). Compliance standards
   describing what the TARGET system should obey are NOT this —
   those are knowledge (fact_shape + proc + conc).
   - extract.rhema_rule body: {{name, action: deny|approval|rewrite,
     layer: reply|ask|url|..., pattern, rationale}}
   - extract.bulwark_rule body: {{name, action: deny|approval|allow,
     layer: fs|exec|net|url|..., match, scope: weapon, rationale}}
   - extract.walls_path body: {{name, kind: deny_path|deny_command|
     approval_path, value, rationale}}

3. Emit one extract.* call with forge_id="{forge_id}", the exact
   manifest_element_id, and a structured body that captures the real
   content. Each extraction body must contain real fields drawn from the
   element's text — not summaries, not paraphrases of instructions.

Constraints:
- Use the exact manifest_element_ids listed above. Do not invent ids.
- Every forge MCP tool call must include forge_id="{forge_id}" in its
  arguments, including mats.get, mats.search, and all extract.* calls.
- Emit at least {min_extractions} extractions this turn.
- Knowledge channels (proc/conc/fact_shape/store/line) are the
  default and what most elements should produce. Policy channels
  (rhema_rule/bulwark_rule/walls_path) are for agent self-discipline
  ONLY — expect zero or near-zero policy emissions from compliance-
  document mats.
- Engagement scope (where/when/against whom) is trim, not weapon-
  policy. Do not emit scope rules through the policy channels.
- No prose preamble. Begin tool-calling immediately.
"""


@dataclass
class IterationResult:
    iteration: int
    pending_before: int
    pending_after: int
    extractions_added: int
    coverage_before: float
    coverage_after: float
    duration_s: float
    rc: int
    error: Optional[str] = None


@dataclass
class DriverResult:
    forge_id: str
    iterations: list[IterationResult] = field(default_factory=list)
    final_coverage: float = 0.0
    final_extractions: int = 0
    stopped_reason: str = ""
    target_coverage: float = 0.0


def _read_extractions_count(state_root: Path, forge_id: str) -> int:
    p = fstate.forge_dir(state_root, forge_id) / "extractions.jsonl"
    if not p.is_file():
        return 0
    try:
        with p.open() as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _build_prompt(
    forge_id: str,
    iteration: int,
    cov: CoverageState,
    batch: list[ManifestElement],
    *,
    min_extractions: int,
    template: str,
    extra_fields: Optional[dict] = None,
) -> str:
    """Render the per-iteration prompt with the batch interpolated.

    Each batch element gets a one-liner: id, kind, source_mat,
    truncated text preview so the agent has enough context to decide
    which alphabet fits without immediately calling mats.get for
    every single one.
    """
    lines = []
    for el in batch:
        preview = (el.extracted_text or "").replace("\n", " ").strip()
        if len(preview) > 240:
            preview = preview[:237] + "..."
        lines.append(
            f"- {el.id}  [{el.kind.value}, mat {el.source_mat_id}]  {preview}"
        )
    fields = dict(
        forge_id=forge_id,
        iteration=iteration,
        coverage_pct=cov.fraction * 100.0,
        pending_total=len(cov.pending),
        manifest_total=cov.total_elements,
        element_block="\n".join(lines),
        min_extractions=min_extractions,
    )
    if extra_fields:
        fields.update(extra_fields)
    return template.format(**fields)


def _default_agent_invoke(
    state_root: Path,
    prompt: str,
    *,
    timeout_s: float = 600.0,
    extra_env: Optional[dict] = None,
) -> int:
    """Spawn `ara launch --contain -- hermes -z <prompt>` with the env
    every contained-hermes-with-MCP-bridge session needs.

    Returns the subprocess exit code. Output is teed to stderr so
    operators see live progress.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    pythonpath = here  # the src/ root so `python -m ara.cli` resolves
    mitm_ca = state_root / "mitm" / "ca.crt"
    hermes_bin = shutil.which("hermes")
    if hermes_bin is None:
        # Try the canonical install path
        candidate = Path.home() / ".local" / "bin" / "hermes"
        if candidate.is_file():
            hermes_bin = str(candidate)
    if hermes_bin is None:
        sys.stderr.write(
            "[forge-loop] hermes binary not found on PATH or in ~/.local/bin\n"
        )
        return 127

    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
    if mitm_ca.is_file():
        env.setdefault("SSL_CERT_FILE", str(mitm_ca))
        env.setdefault("REQUESTS_CA_BUNDLE", str(mitm_ca))
    if extra_env:
        env.update(extra_env)

    cmd = [
        sys.executable, "-m", "ara.cli", "launch",
        "--contain", "--contain-profile", "forge",
        "--state-root", str(state_root),
        "--", hermes_bin, "-z", prompt,
    ]
    sys.stderr.write(
        f"[forge-loop] invoke: {shlex.join(cmd[:10])} ... -z <{len(prompt)} chars>\n"
    )
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            check=False,
            timeout=timeout_s,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write("[forge-loop] timeout — killing agent\n")
        return -1
    if proc.stderr:
        # Trim noise; keep only [ara]/error lines for the operator
        for line in proc.stderr.splitlines()[-20:]:
            sys.stderr.write(f"  | {line}\n")
    return proc.returncode


def _read_turn_status(status_path: Path) -> bool:
    """The agent's batch_complete signal for the last turn. Absent/unreadable
    → True (advance), so a silent agent behaves like the one-shot default."""
    import json as _json
    try:
        return bool(_json.loads(status_path.read_text()).get("batch_complete", True))
    except Exception:  # noqa: BLE001
        return True


def _select_batch(remaining, elements_by_id, batch_size):
    """Pick the next batch, isolating table-dense elements into a batch of one.

    A "[reconstructed table]" grid needs the agent's full attention: batching
    it alongside prose starves it (proven on dense CVSS/matrix content, where
    batch>1 landed zero extractions while batch=1 converted every table). So a
    dense element runs solo; prose elements group up to ``batch_size``, but the
    group stops before the next dense element rather than swallowing it."""
    def _dense(eid):
        el = elements_by_id.get(eid)
        text = (el.extracted_text or "") if el else ""
        if "[reconstructed table]" in text:      # pdfplumber-recovered PDF table
            return True
        # markdown value-tables (e.g. a CVSS mat) carry no marker but are just
        # as attention-heavy — three or more pipe-delimited rows is a table.
        pipe_rows = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("|"))
        return pipe_rows >= 3

    if not remaining:
        return []
    if _dense(remaining[0]):
        return [remaining[0]]
    picked = []
    for eid in remaining:
        if _dense(eid):
            break
        picked.append(eid)
        if len(picked) >= batch_size:
            break
    return picked


def run_exhaust(
    state_root: Path,
    forge_id: str,
    *,
    target_coverage: float = 0.50,
    batch_size: int = 15,
    max_iterations: int = 50,
    min_extractions_per_iter: int = 3,
    iteration_timeout_s: float = 600.0,
    no_progress_bail: int = 3,
    prompt_template: str = _DEFAULT_BATCH_PROMPT_TEMPLATE,
    agent_invoke: Callable[[Path, str], int] | None = None,
    prompt_context_fn: Optional[Callable[[Path, str], dict]] = None,
    recycle_batches: bool = False,
    max_cycles_per_batch: int = 6,
) -> DriverResult:
    """Drive the agent through the manifest until target coverage hits.

    Returns a :class:`DriverResult` summarising every iteration. Does
    not raise on per-iteration agent failures — it continues, logs the
    rc, and gives up only after two consecutive zero-progress runs.
    """
    state_root = Path(state_root)
    sess = ForgeSession.load(state_root, forge_id)
    if sess.phase != ForgePhase.EXHAUST:
        raise RuntimeError(
            f"forge {forge_id} is in phase {sess.phase.value}, not EXHAUST"
        )
    if agent_invoke is None:
        agent_invoke = lambda sr, pr: _default_agent_invoke(  # noqa: E731
            sr, pr, timeout_s=iteration_timeout_s,
        )

    manifest_path = fstate.forge_manifest_path(state_root, forge_id)
    elements_by_id = {e.id: e for e in read_manifest(manifest_path)}

    manifest_ids = list(elements_by_id.keys())   # manifest order
    result = DriverResult(forge_id=forge_id, target_coverage=target_coverage)
    consecutive_no_progress = 0
    # Elements fully processed. Non-recycle: an element is done after one shot
    # (element-advance — un-extractable head elements can't loop and stall the
    # run). Recycle: an element/batch stays until the AGENT declares it done
    # (exhaust.status) or a cycle yields no new proposals — agent-driven
    # completion, not a wall-clock timeout.
    done: set = set()
    batch_cycles: dict = {}
    status_path = fstate.forge_turn_status_path(state_root, forge_id)

    for iteration in range(1, max_iterations + 1):
        cov_before = coverage_state(sess)
        if cov_before.fraction >= target_coverage:
            result.stopped_reason = "target_coverage_reached"
            break
        # Recycle re-shows an element until it's exhausted, so it selects over
        # ALL not-yet-done elements; non-recycle only chases uncovered ones.
        source_ids = manifest_ids if recycle_batches else cov_before.pending
        remaining = [i for i in source_ids if i not in done]
        if not remaining:
            result.stopped_reason = "all_elements_attempted"
            break

        batch_ids = _select_batch(remaining, elements_by_id, batch_size)
        if not recycle_batches:
            done.update(batch_ids)     # element-advance: one shot each
        batch = [elements_by_id[i] for i in batch_ids if i in elements_by_id]
        if not batch:
            result.stopped_reason = "manifest_id_mismatch"
            break
        # clear any stale turn-status before the agent runs
        try:
            status_path.unlink()
        except OSError:
            pass

        extra_fields = (
            prompt_context_fn(state_root, forge_id)
            if prompt_context_fn else None
        )
        prompt = _build_prompt(
            forge_id, iteration, cov_before, batch,
            min_extractions=min_extractions_per_iter,
            template=prompt_template,
            extra_fields=extra_fields,
        )
        extractions_before = _read_extractions_count(state_root, forge_id)
        sys.stderr.write(
            f"\n[forge-loop] iter {iteration}/{max_iterations} | "
            f"coverage {cov_before.fraction*100:.1f}% "
            f"({cov_before.elements_with_extractions}/{cov_before.total_elements}) | "
            f"pending {len(cov_before.pending)} | batch {len(batch)}\n"
        )
        t0 = time.time()
        try:
            rc = agent_invoke(state_root, prompt)
            err = None
        except Exception as e:  # noqa: BLE001
            rc = -2
            err = f"{type(e).__name__}: {e}"
        dur = time.time() - t0
        extractions_after = _read_extractions_count(state_root, forge_id)
        cov_after = coverage_state(sess)
        added = extractions_after - extractions_before
        progress = cov_after.elements_with_extractions \
                   - cov_before.elements_with_extractions

        result.iterations.append(IterationResult(
            iteration=iteration,
            pending_before=len(cov_before.pending),
            pending_after=len(cov_after.pending),
            extractions_added=added,
            coverage_before=cov_before.fraction,
            coverage_after=cov_after.fraction,
            duration_s=dur,
            rc=rc,
            error=err,
        ))
        sys.stderr.write(
            f"[forge-loop] iter {iteration} done | "
            f"+{added} extractions | "
            f"+{progress} elements covered | "
            f"coverage now {cov_after.fraction*100:.1f}% | "
            f"rc={rc} | {dur:.1f}s\n"
        )
        # Recycle: advance the batch only when the AGENT says it's done, or a
        # cycle produced nothing new, or the safety cap is hit (a "more" that
        # isn't actually deriving anything — a progress gate, not a time gate).
        if recycle_batches:
            bkey = tuple(batch_ids)
            batch_cycles[bkey] = batch_cycles.get(bkey, 0) + 1
            batch_complete = _read_turn_status(status_path)
            _advanced = (batch_complete or added == 0
                    or batch_cycles[bkey] >= max_cycles_per_batch)
            sys.stderr.write(
                f"[cycle-probe] iter {iteration} cycle={batch_cycles[bkey]} "
                f"batchsz={len(batch_ids)} added={added} "
                f"complete={bool(batch_complete)} advanced={bool(_advanced)}\n"
            )
            if _advanced:
                done.update(batch_ids)
            # else: same batch is re-selected next iteration (another cycle)

        # Element-advance already guarantees termination (all elements get one
        # shot, then "all_elements_attempted"), so no-progress is only a bail
        # once there's nothing left to try — never while extractable elements
        # remain unattempted (that was the 68%-stall bug).
        source_after = manifest_ids if recycle_batches else coverage_state(sess).pending
        remaining_after = [i for i in source_after if i not in done]
        if progress == 0 and not remaining_after:
            consecutive_no_progress += 1
            if consecutive_no_progress >= no_progress_bail:
                result.stopped_reason = (
                    f"{no_progress_bail}_consecutive_no_progress"
                )
                break
        else:
            consecutive_no_progress = 0

    final_cov = coverage_state(sess)
    result.final_coverage = final_cov.fraction
    result.final_extractions = _read_extractions_count(state_root, forge_id)
    if not result.stopped_reason:
        result.stopped_reason = "max_iterations_reached"
    return result


_PACK_AGENT_PROMPT_TEMPLATE = """\
You are Codex with the Pen equipped, inside ARA. Forge session {forge_id}
is in EXHAUST phase (iteration {iteration}, coverage {coverage_pct:.1f}%,
{pending_total}/{manifest_total} elements pending).

Your sole job: forge an ALPHABET PACK — the controlled vocabulary a
pentest agent classifies findings by. This is an EXHAUSTIVE source; the
pack is built cumulatively across many turns.

ALPHABETS ALREADY IN THIS PACK (reuse these names EXACTLY and add entries to
them; declare a new alphabet only for a genuinely new dimension):
{pack_state}

THIS turn's manifest elements:

{element_block}

Work through each listed element EXACTLY ONCE, in order:
1. Call mcp_ara_forge_mats_get ONCE for that id (forge_id="{forge_id}"). You
   now have its full text — do NOT call mats.get or mats.search again for it.
2. Propose vocabulary grounded in it — but REUSE BEFORE YOU DECLARE. For most
   elements the right move is adding ENTRIES to alphabets that ALREADY EXIST,
   not minting new alphabets. Before declaring anything, scan "ALPHABETS
   ALREADY IN THIS PACK": if this element's dimension is already represented
   (even loosely — a vuln type, an access level, a test category, a MITRE
   technique, a crypto weakness), ADD ENTRIES to that alphabet via extract.conc.
   Declare a NEW alphabet ONLY when the dimension is genuinely absent.
   RIGHT SCALE: a good pack for this whole corpus is on the order of 20-40
   broad, reusable alphabets with MANY entries each — NOT hundreds of thin ones.
   Warning signs you are over-fragmenting (stop and reuse instead): you are
   declaring a new alphabet on most elements; the pack already lists 40+
   alphabets; or your "new" alphabet is a narrower slice of an existing one
   (e.g. "wifi_vuln" when vulnerability_class exists — that is an ENTRY, not a
   new alphabet). A handful of big alphabets (vulnerability_class, access_vector,
   attack_surface_layer, test_category, mitre_technique, ...) should absorb the
   large majority of what you find.
   DISTINCT AXES, still: keep genuinely different dimensions apart — a "crypto
   weakness" is a different axis from an "access vector", so do not collapse
   everything into one catch-all. The bar for a NEW alphabet is "a new AXIS",
   not "a new value" and not "different wording for an existing axis"
   ("wstg_test_category" vs "test_category" is a DUPLICATE — reuse it).
   GRANULARITY floor: a genuine dimension with only 1 member is fine to hold
   for now; do not invent members, but do not refuse to declare a real axis
   just because you have seen one value so far.
   SHAPE CHECK — before defaulting to categorical, look at the structure. The
   non-categorical shapes REQUIRE shape-native fields in propose.alphabet or
   the call is rejected — declaring the shape but omitting them is an error:
   - Values looked up by one-or-more DIMENSIONS (a table: metric × value →
     weight, likelihood × impact → risk)? → shape "matrix". propose.alphabet
     body MUST carry axes:[dimension_name, ...] and cell_fields:[{{name,type}}].
     Then emit each cell as extract.conc with body {{alphabet_name,
     coords:{{<axis>:<value>,...}}, cell:{{<cell_field>:<value>,...}}}}.
     A "[reconstructed table]" or markdown grid is a STRONG matrix signal —
     its header columns are axes, its body rows are cells.
   - A set of SCORED dimensions / a graded vector (CVSS metric vector, a set
     of severity axes each with a range)? → shape "vector". propose.alphabet
     body MUST carry axes:[{{name, min, max, description}}]. A vector ships its
     axes — do NOT send entry rows for it.
   - A reusable TEMPLATE with slots (a fix template, report/test-case
     template)? → shape "template_set" (body: fields). Populate with
     extract.conc entry rows.
   - Otherwise a flat set of values → shape "categorical" (body: {{name,
     shape:"categorical", fields:[{{name:"id",type:"str"}},
     {{name:"label",type:"str"}}], description}}). Populate with extract.conc
     entry rows.
   Do not flatten a genuine table/scale/template into a categorical list —
   use the shape that matches, and give it the required native fields. Most
   vocab is categorical; the others are less common but real, so reach for
   them (fully-formed) when the structure is there.
   Populate categorical/template_set alphabets with entries:
   - extract.conc — a concrete entry. body: {{alphabet_name, entry:{{id, label}}}}
   - propose.binding — tie a finding field to an alphabet.
     body: {{field, rule:"FIELD_IN_ALPHABET", alphabet}}
{policy_step}3. Move to the next element.

Discipline (critical — a prior run looped and was killed):
- ONE mats.get per element. Never re-read or re-search an element.
- Declare each alphabet ONCE. Reuse names from "ALPHABETS ALREADY IN THIS
  PACK" and this turn; never duplicate a dimension under a new name.
- First pass is the pass. Do not re-verify, second-guess, or re-emit.
- Every channel call includes forge_id="{forge_id}" and an exact
  manifest_element_id from the list. Do not invent ids. Do not fabricate.
- Emit at least {min_extractions} proposals this turn.

END OF TURN: when you have proposed all you can this turn, call exhaust.status
with forge_id="{forge_id}" and:
- batch_complete=true if you have FULLY derived everything from these elements
  (the driver moves on to the next elements), OR
- batch_complete=false if there is MORE to derive from these SAME elements and
  you want another cycle on them (the driver will show them to you again).
Then stop — make no further calls. This is how YOU decide when an element is
exhausted; there is no time limit. Do not claim more when there is nothing left.

No prose preamble. Begin tool-calling immediately.
"""


_PACK_POLICY_STEP = """\
2b. POLICY (agent self-discipline) — ONLY where THIS element EXPLICITLY marks a
   test/action as destructive, dangerous, or handling sensitive data. Prefer a
   mechanical block; fall back to advisory. Expect few or none per element.
   - If the mat names a CONCRETE destructive command/pattern (a table-dropping
     SQL statement, mass delete, rm -rf), block it mechanically:
     extract.bulwark_rule body: {{name, action:"deny", layer:"exec"|"net"|"fs",
     match:<the concrete pattern>, scope:"weapon", rationale:<what the mat says>}}
   - Otherwise, softer "avoid X" guidance → advisory rhema:
     extract.rhema_rule body: {{name, action:"deny"|"rewrite",
     layer:"reply"|"ask", pattern:<what to avoid>, rationale:<what the mat says>}}
   Do NOT turn a TARGET security requirement into agent policy — only things the
   mat flags as dangerous for the TESTER to do.
"""


def _pack_state_primer(state_root: Path, forge_id: str) -> dict:
    """Compact summary of the pack so far, for cross-turn coherence. Lists each
    declared alphabet (name, shape, one-line description) and its entry COUNT —
    never the entries themselves, so the primer stays roughly flat as the pack
    grows over many turns (honoring the context-overload constraint). Returned
    as ``{"pack_state": <text>}`` for prompt interpolation."""
    from .phases.exhaust import read_extractions
    from .session import ForgeSession
    # policy step is opt-out per session (config.forge_policy)
    try:
        forge_policy = ForgeSession.load(state_root, forge_id).config.forge_policy
    except Exception:  # noqa: BLE001
        forge_policy = True
    policy_step = _PACK_POLICY_STEP if forge_policy else ""
    alphabets: dict = {}   # name -> {shape, description}
    counts: dict = {}      # alphabet_name -> entry count
    for ev in read_extractions(state_root, forge_id):
        body = ev.body or {}
        if ev.channel == "propose.alphabet":
            nm = str(body.get("name") or "").strip()
            if nm and nm not in alphabets:
                alphabets[nm] = {
                    "shape": str(body.get("shape") or "categorical"),
                    "description": str(body.get("description") or "")[:90],
                }
        elif ev.channel == "extract.conc":
            nm = str(body.get("alphabet_name") or "").strip()
            if nm:
                counts[nm] = counts.get(nm, 0) + 1
    if not alphabets:
        return {"pack_state": "(none yet — you are starting the pack)",
                "policy_step": policy_step}
    # Names/shapes/counts ONLY — no descriptions. The primer's job is dedup
    # (so the agent reuses existing names); the name is what it matches on, and
    # descriptions ballooned the per-turn prompt (~90 chars x N alphabets) with
    # zero benefit to reuse. Keeps the primer compact as the pack grows.
    lines = []
    for nm, meta in alphabets.items():
        lines.append(
            f"- {nm} ({meta['shape']}, {counts.get(nm, 0)} entries)"
        )
    return {"pack_state": "\n".join(lines), "policy_step": policy_step}


def run_pack_exhaust(
    state_root: Path,
    forge_id: str,
    *,
    agent_invoke: Optional[Callable[[Path, str], int]] = None,
    iteration_timeout_s: float = 600.0,
    **kw,
) -> DriverResult:
    """Agentic pack exhaust: the contained hermes agent (codex+pen) proposes
    the pack vocabulary via the propose.* channels. Thin wrapper over
    :func:`run_exhaust` with the pack prompt and ``ARA_FORGE_MCP_MODE=pack``
    so the agent sees only pack tools.
    """
    if agent_invoke is None:
        agent_invoke = lambda sr, pr: _default_agent_invoke(  # noqa: E731
            sr, pr, timeout_s=iteration_timeout_s,
            extra_env={"ARA_FORGE_MCP_MODE": "pack"},
        )
    # Recycle batches: the agent declares batch_complete (exhaust.status) and
    # the driver cycles the same elements until it says done — the wall-clock
    # is only a hang-safety, not the normal stop.
    kw.setdefault("recycle_batches", True)
    return run_exhaust(
        state_root, forge_id,
        prompt_template=_PACK_AGENT_PROMPT_TEMPLATE,
        agent_invoke=agent_invoke,
        iteration_timeout_s=iteration_timeout_s,
        prompt_context_fn=_pack_state_primer,
        **kw,
    )


__all__ = [
    "DriverResult",
    "IterationResult",
    "run_exhaust",
    "run_pack_exhaust",
    "run_reconcile",
    "run_workflow_derive",
]


# --- RECONCILE (reduce-step multi-source reconciliation) -------------------
# build_pack_draft unions extractions across mats naively, so a concept that
# appears incompatibly in two mats seals incoherent (contradictory matrix
# cells) or as semantic-duplicate alphabets. The agent got this wrong during
# EXHAUST because it was BLIND: batched turns + a compact primer (no existing
# cells), so it couldn't see the contradiction it was creating. RECONCILE fixes
# it at the reduce, where the agent gets each conflict-group's FULL content
# WITH the source behind every cell, scoped one group per turn.

_RECONCILE_PROMPT_TEMPLATE = """\
You are reconciling a forged security "alphabet pack". A mechanical check found
a conflict caused by aggregating multiple source documents (mats). Fix ONLY
this conflict, using the reconcile.* tools, then stop.

forge_id: {forge_id}

CONFLICT ({kind}):
{body}

How to decide (prefer the earliest that fits):
- If two alphabets are the SAME concept under different names → reconcile.merge
  {{targets:[names], into:<best name>, rationale}}.
- If one matrix mixes TWO genuinely different scales/tables from different
  sources (different mats, incompatible values) → reconcile.split
  {{name, into:[{{new_name:"<name>__<source>", element_ids:[...]}}, ...],
  rationale}}. Put each source's element_ids in its own new alphabet.
- If a single coordinate has conflicting values but they are the SAME scale and
  one is an extraction error/typo → reconcile.resolve_cell
  {{alphabet, coords, keep_cell, rationale}}. The kept value MUST be one shown
  below (from a real mat) — never invent a compromise.
- Only if the content is genuinely unusable → reconcile.drop {{name, rationale}}.

Every call includes forge_id="{forge_id}". When done, call exhaust.status with
forge_id="{forge_id}" and batch_complete=true.
"""


def _read_decisions(path) -> list:
    import json as _json
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(_json.loads(line))
            except ValueError:
                pass
    return out


def _reconcile_group_body(group: dict, draft, source_of) -> str:
    """Render a conflict group with full content + per-cell source for the
    agent. ``source_of`` maps manifest_element_id -> source label (mat)."""
    from ara.forge import reconcile as _rec
    lines: list = []
    if group["kind"] == "contradiction":
        name = group["alphabet"]
        decl = _rec._alpha_decls(draft).get(name, {}).get("data", {})
        lines.append(f"matrix alphabet: {name}")
        lines.append(f"  axes: {decl.get('axes')}  cell_fields: "
                     f"{decl.get('cell_fields') or decl.get('fields')}")
        for cf in group["conflicts"]:
            lines.append(f"  coord {cf['coords']} has CONFLICTING values:")
            for v in cf["values"]:
                srcs = sorted({source_of.get(c.get("manifest_element_id"),
                                             c.get("manifest_element_id"))
                               for c in v["cites"]})
                lines.append(f"    - {v['cell']}   from source(s): {srcs}")
        # also list element_ids per source in case a split is warranted
        lines.append("  (to split: group these element_ids by source)")
        for it in _rec._entry_items(draft, name):
            e = (it.get("data") or {}).get("entry") or {}
            eid = (it.get("cite") or {}).get("manifest_element_id")
            src = source_of.get(eid, eid)
            lines.append(f"    element {eid} [{src}]: "
                         f"{e.get('coords')} -> {e.get('cell')}")
    else:  # duplicate
        lines.append(f"these {len(group['alphabets'])} alphabets look like the "
                     f"same concept: {group['alphabets']}")
        for name in group["alphabets"]:
            decl = _rec._alpha_decls(draft).get(name, {}).get("data", {})
            ents = _rec._entry_items(draft, name)
            lines.append(f"  {name} ({decl.get('shape')}, {len(ents)} entries): "
                         f"{decl.get('description','')[:80]}")
    return "\n".join(lines)


def run_reconcile(
    state_root: Path,
    forge_id: str,
    *,
    agent_invoke: Optional[Callable[[Path, str], int]] = None,
    iteration_timeout_s: float = 300.0,
    max_passes: int = 2,
) -> dict:
    """Agent-driven reduce-step reconciliation. Detect conflict-groups, run one
    contained (reconcile-mode) agent turn per group with full content + source,
    apply the emitted decisions, and re-check. Unresolved groups after
    ``max_passes`` are dropped (last resort) so seal always converges."""
    state_root = Path(state_root)
    from ara.forge.phases import synthesize as _syn
    from ara.forge import reconcile as _rec
    draft = _syn.load_pack_draft(state_root, forge_id)
    if draft is None:
        return {"groups": 0, "decisions": 0, "unresolved_dropped": 0}

    manifest_path = fstate.forge_manifest_path(state_root, forge_id)
    source_of = {e.id: e.source_mat_id for e in read_manifest(manifest_path)}
    dec_path = fstate.forge_reconcile_decisions_path(state_root, forge_id)

    if agent_invoke is None:
        agent_invoke = lambda sr, pr: _default_agent_invoke(  # noqa: E731
            sr, pr, timeout_s=iteration_timeout_s,
            extra_env={"ARA_FORGE_MCP_MODE": "reconcile"},
        )

    groups_handled = 0
    decisions_applied = 0
    for _pass in range(max_passes):
        groups = _rec.detect_conflict_groups(draft)
        if not groups:
            break
        sys.stderr.write(
            f"\n[reconcile] pass {_pass + 1}: {len(groups)} conflict group(s)\n")
        for group in groups:
            before = len(_read_decisions(dec_path))
            prompt = _RECONCILE_PROMPT_TEMPLATE.format(
                forge_id=forge_id, kind=group["kind"],
                body=_reconcile_group_body(group, draft, source_of),
            )
            try:
                agent_invoke(state_root, prompt)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[reconcile] agent error: {e}\n")
            for d in _read_decisions(dec_path)[before:]:
                try:
                    _rec.apply_decision(draft, d)
                    decisions_applied += 1
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"[reconcile] bad decision {d}: {e}\n")
            groups_handled += 1
        _syn.save_pack_draft(state_root, forge_id, draft)

    # Acceptance: only CONTRADICTIONS block seal (_validate_pack fails on
    # conflicting cells), so only those are force-dropped as a last resort.
    # Unresolved DUPLICATES are a quality nit, not a seal-blocker — leave them
    # (dropping a split-sibling here would partly undo a good split).
    unresolved = [g for g in _rec.detect_conflict_groups(draft)
                  if g["kind"] == "contradiction"]
    dropped = 0
    for group in unresolved:
        _rec.apply_drop(draft, group["alphabet"])
        dropped += 1
        sys.stderr.write(
            f"[reconcile] UNRESOLVED contradiction -> dropped "
            f"{group['alphabet']!r}\n")
    _syn.save_pack_draft(state_root, forge_id, draft)
    return {"groups": groups_handled, "decisions": decisions_applied,
            "unresolved_dropped": dropped}


# --- WORKFLOW derivation (forge the investigation chunk graph from mats) ----
# The chunk graph is the investigation WORKFLOW (enum→probe→…), which standard
# security methodologies (WSTG web, ISTG IoT, MASVS mobile) describe directly —
# so it should be DERIVED from the mats, not hand-authored. This runs a focused
# contained agent turn that reads methodology content from the forge's mats and
# emits a complete, coherent chunk graph via propose.chunk (grounding each node
# in a methodology element, using its own methodology knowledge for gaps).

_WORKFLOW_DERIVE_PROMPT = """\
You are deriving the INVESTIGATION WORKFLOW for a pentest engagement — the
chunk graph a testing agent walks: a small set of phases (enumerate → probe →
… → write up → operator review), each a node with an id, title, a
prompt_fragment (what the agent does in that phase), a tool_surface, exits
(tag → next node), gems, and is_entry / is_terminal / can_emit_finding flags.

forge_id: {forge_id}
target workflow: {domain}

Do this:
1. Search the mats (mats.search / mats.get) for TESTING-METHODOLOGY content —
   testing phases, engagement stages, a testing checklist's structure (e.g. a
   web app testing guide, an IoT/hardware testing guide, a mobile testing
   guide). Ground the workflow in what the mats actually describe.
2. Where the mats are thin, complete the workflow from the STANDARD methodology
   for this domain (your own knowledge — WSTG phases, OWASP ISTG stages, MASVS,
   hardware teardown steps). Do not invent exotic phases.
3. Emit each phase as ONE propose.chunk call. The body is a full chunk node:
   {{id, title, prompt_fragment, tool_surface:[tool names], exits:{{"<tag>":
   {{tag, next_chunk_id, requires_caller, description}}}}, gems, is_entry,
   is_terminal, can_emit_finding}}. Cite the methodology element you grounded
   on in manifest_element_id. Model tool_surface and gems on a standard loop.

Constraints (the graph is validated):
- EXACTLY ONE node has is_entry=true.
- End with a single terminal node (is_terminal=true) — the operator review.
- Every exit's next_chunk_id must be another node's id (no dangling exits).
- The write-up phase sets can_emit_finding=true; earlier phases do not.
- Keep it to ~4-7 phases. Coherent and connected, not a pile of nodes.

Every propose.chunk includes forge_id="{forge_id}". When the whole graph is
emitted, call exhaust.status with forge_id="{forge_id}" and batch_complete=true.
"""


def run_workflow_derive(
    state_root: Path,
    forge_id: str,
    *,
    name: str,
    domain: Optional[str] = None,
    description: Optional[str] = None,
    agent_invoke: Optional[Callable[[Path, str], int]] = None,
    iteration_timeout_s: float = 400.0,
) -> dict:
    """Derive a chunk workflow from the forge's mats via a contained agent turn,
    validate the graph, and save it to the forged-workflows registry. Returns
    ``{name, nodes, saved, failures}``."""
    state_root = Path(state_root)
    from ara.forge import chunk_workflows as _cw
    from ara.forge.phases.exhaust import read_extractions

    before = sum(1 for e in read_extractions(state_root, forge_id)
                 if e.channel == "propose.chunk")
    if agent_invoke is None:
        agent_invoke = lambda sr, pr: _default_agent_invoke(  # noqa: E731
            sr, pr, timeout_s=iteration_timeout_s,
            extra_env={"ARA_FORGE_MCP_MODE": "workflow"},
        )
    prompt = _WORKFLOW_DERIVE_PROMPT.format(
        forge_id=forge_id, domain=domain or name)
    try:
        agent_invoke(state_root, prompt)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[workflow] agent error: {e}\n")

    chunks = [e.body for e in read_extractions(state_root, forge_id)
              if e.channel == "propose.chunk"][before:]
    fails = _cw.validate_chunk_graph(chunks)
    if fails:
        sys.stderr.write(f"[workflow] derived graph invalid: {fails}\n")
        return {"name": name, "nodes": len(chunks), "saved": False,
                "failures": fails}
    _cw.save_workflow(state_root, name,
                      description or f"Forged from mats ({domain or name}).",
                      chunks)
    return {"name": name, "nodes": len(chunks), "saved": True, "failures": []}


# --- stateless per-element extractor ---------------------------------------
# The agentic run_exhaust (above) runs one long contained-hermes turn per
# batch: it re-reads each element via MCP and the growing conversation is
# re-sent every step, so late steps drag and turns time out. EXHAUST is a MAP
# — each element yields extractions independently — so this driver makes ONE
# direct model call per element (constant context), grounds every extraction to
# the element under examination (record_extraction verifies the id against the
# manifest — no fabrication), and records via the same shallow-validated
# extract.* path. SYNTHESIZE (a reduce over all extractions) is untouched.

from uuid import uuid4

_STATELESS_EXHAUST_KINDS = {
    "proc", "conc", "fact_shape", "store", "resource", "line",
    "rhema_rule", "bulwark_rule", "walls_path",
}

_STATELESS_EXHAUST_SYSTEM = """\
You extract structured content from ONE security source element for a weapon
"alphabet" (what the testing agent tests FOR). Read the element and output a
JSON array of extractions (most elements yield 1-3; pure navigation text like
title pages / tables of contents / bare headers yields []).

Each extraction: {"kind": <one below>, "body": {<real fields from the text>}}

Knowledge alphabets — THE DEFAULT. PRIORITIZE "proc": most security material
describes HOW to test something, and proc is the weapon's core capability (it
becomes the weapon's test procedures / crumbs). Emit a "proc" for ANY element
describing a test, check, method, technique, or ordered step sequence — even a
short one. Then add the others where they fit:
 - "proc" (PREFER — emit whenever the text implies a test/check/method): a procedure/step pattern. body: {"name","steps":[...],"requires":[...],"emits":[...]}
 - "conc": a conceptual shape / template / rubric / matrix. body: {"name","fields":[...]}
 - "fact_shape": a structured assertion schema. body: {"name","fields":[...]}
 - "store": an internal fact/evidence store entry. body: {"name","value":...}
 - "resource": an external reference/wordlist/script/tool/vendor_doc/vuln_db. body: {"id","class","name","locator" (or "hints" if no verified URL),"rationale"}
 - "line": an in-scope / out-of-scope boundary. body: {"name","in_scope":[...],"out_of_scope":[...],"rationale"}

Agent self-discipline — ONLY for text about how the TESTING AGENT itself should
behave (throttling, credential redaction, approval gates on destructive
commands). Compliance text describing what the TARGET must obey is knowledge,
NOT policy — expect near-zero policy extractions.
 - "rhema_rule": {"name","action":"deny|approval|rewrite","layer":"reply|ask|url","pattern","rationale"}
 - "bulwark_rule": {"name","action":"deny|approval|allow","layer":"fs|exec|net|url","match","scope":"weapon","rationale"}
 - "walls_path": {"name","kind":"deny_path|deny_command|approval_path","value","rationale"}

Derive ONLY from the element text shown. Do NOT call tools. Do NOT fabricate.
Output ONLY the JSON array (or [])."""


@dataclass
class StatelessExhaustResult:
    forge_id: str
    elements_total: int = 0
    elements_examined: int = 0
    extractions_added: int = 0
    final_coverage: float = 0.0
    stopped_reason: str = ""


def _stateless_model_invoke(
    system: str, user: str, *, endpoint: str | None = None,
    model: str = "qwen/qwen3.6-35b-a3b", timeout_s: float = 90.0,
) -> str:
    import json
    import urllib.request

    url = (
        endpoint
        or os.environ.get("ARA_FORGE_MODEL_ENDPOINT")
        or os.environ.get("ARA_TEMPER_MODEL_ENDPOINT")
        or "http://127.0.0.1:1234/v1/chat/completions"
    )
    body = json.dumps({
        "model": model, "temperature": 0.2, "max_tokens": 1400,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read())
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def _parse_json_array(text: str) -> list:
    import json

    t = str(text or "").replace("```json", "").replace("```", "").strip()
    i, j = t.find("["), t.rfind("]")
    if 0 <= i < j:
        try:
            data = json.loads(t[i:j + 1])
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(t[i:j + 1])
            if isinstance(obj, dict):
                return [obj]
        except Exception:  # noqa: BLE001
            pass
    return []


def run_exhaust_stateless(
    state_root: Path,
    forge_id: str,
    *,
    max_elements: int | None = None,
    element_text_cap: int = 6000,
    model_invoke: Callable[[str, str], str] | None = None,
    model_endpoint: str | None = None,
    model_name: str = "qwen/qwen3.6-35b-a3b",
    timeout_s: float = 90.0,
) -> StatelessExhaustResult:
    """Stateless map over the manifest: one direct model call per element,
    constant context. Each extraction is grounded to the element under
    examination (``record_extraction`` verifies the id against the manifest)
    and written via the same ``extract.*`` path. Requires EXHAUST phase.
    SYNTHESIZE is unaffected — this only replaces the exhaust extraction loop.
    """
    state_root = Path(state_root)
    sess = ForgeSession.load(state_root, forge_id)
    if sess.phase != ForgePhase.EXHAUST:
        raise RuntimeError(
            f"forge {forge_id} is in phase {sess.phase.value}, not EXHAUST"
        )
    manifest_path = fstate.forge_manifest_path(state_root, forge_id)
    elements_by_id = {
        e.id: e for e in read_manifest(manifest_path)
        if not getattr(e, "dropped_by_operator", False)
    }
    # Process only elements without an extraction yet, so repeated runs advance
    # coverage instead of re-doing the same head of the manifest.
    pending = coverage_state(sess).pending
    elements = [elements_by_id[i] for i in pending if i in elements_by_id]
    if max_elements is not None:
        elements = elements[:max_elements]

    invoke = model_invoke or (
        lambda sys_p, usr_p: _stateless_model_invoke(
            sys_p, usr_p, endpoint=model_endpoint,
            model=model_name, timeout_s=timeout_s,
        )
    )

    result = StatelessExhaustResult(
        forge_id=forge_id, elements_total=len(elements))
    for el in elements:
        text = (getattr(el, "extracted_text", "") or "")[:element_text_cap]
        user = (
            f"manifest_element_id: {el.id}\n\n"
            f"ELEMENT TEXT:\n{text}\n\n"
            "Output the JSON array of extractions now."
        )
        try:
            raw = invoke(_STATELESS_EXHAUST_SYSTEM, user)
        except Exception:  # noqa: BLE001 — a per-element failure is non-fatal
            raw = ""
        for item in _parse_json_array(raw):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            kind = kind[len("extract."):] if kind.startswith("extract.") else kind
            body = item.get("body")
            if kind not in _STATELESS_EXHAUST_KINDS or not isinstance(body, dict):
                continue
            try:
                record_extraction(
                    sess,
                    manifest_element_id=el.id,
                    channel=f"extract.{kind}",
                    body=body,
                    extraction_id="sx_" + uuid4().hex[:12],
                )
                result.extractions_added += 1
            except ValueError:
                continue  # grounding/channel rejection — skip
        result.elements_examined += 1

    result.final_coverage = coverage_state(sess).fraction
    result.stopped_reason = "all_elements_examined"
    return result


__all__ = __all__ + ["StatelessExhaustResult", "run_exhaust_stateless"]


# ---- pack-forge stateless exhaust -----------------------------------------
# Pack-forge produces a *vocabulary*, not weapon extractions. The agent reads
# each element and PROPOSES alphabet declarations / entries / bindings via the
# pen weapon's propose.* channels. Same stateless map (one grounded model call
# per element, constant context) — but a pack prompt, and the declared-alphabet
# inventory is threaded into each element's prompt so entries accrete onto
# existing alphabets instead of the model re-inventing a dimension per element.

_PACK_EXHAUST_ALIASES = {
    "alphabet": "propose.alphabet", "propose.alphabet": "propose.alphabet",
    "entry": "extract.conc", "extract.conc": "extract.conc", "conc": "extract.conc",
    "binding": "propose.binding", "propose.binding": "propose.binding",
    "chunk": "propose.chunk", "propose.chunk": "propose.chunk",
    "detection": "propose.detection", "propose.detection": "propose.detection",
}


def _pack_kind_to_channel(kind: object) -> str | None:
    return _PACK_EXHAUST_ALIASES.get(str(kind or "").strip().lower())


_PACK_EXHAUST_SYSTEM = """\
You are forging an ALPHABET PACK: the controlled vocabulary a penetration-
testing agent uses to classify what it tests and finds. Read ONE source element
and propose vocabulary grounded in it. Output a JSON array of proposals (most
substantive elements yield 1-4; pure navigation text like covers / tables of
contents / bare headers yields []).

Each proposal: {"kind": <one below>, "body": {...}}

 - "alphabet": declare a NEW categorical dimension the text reveals — a family
   of things a tester classifies findings by (e.g. a test-category taxonomy, a
   vulnerability-class set, a hardware-interface set, an auth-weakness set).
   Declare one ONLY when the element introduces a dimension not already in
   "Alphabets so far". Keep the count small and reusable.
   body: {"name": snake_case, "shape": "categorical",
          "fields": [{"name":"id","type":"str"},{"name":"label","type":"str"}],
          "description": "one line"}
 - "entry": a concrete value that populates an alphabet — THE MOST COMMON
   proposal. Prefer adding entries to an alphabet already in "Alphabets so far"
   over declaring a new one.
   body: {"alphabet_name": <a declared name>,
          "entry": {"id": snake_case, "label": "human readable"}}
 - "binding": a validation rule tying a finding-record field to an alphabet.
   body: {"field": <finding field>, "rule": "FIELD_IN_ALPHABET", "alphabet": <name>}

Rules:
- Reuse alphabet names from "Alphabets so far" EXACTLY; never rename or
  duplicate a dimension under a new name.
- ids are short snake_case; labels are human-readable. Derive EVERY value from
  the element text shown — do not invent categories the text doesn't support.
- Do NOT call tools. Output ONLY the JSON array (or [])."""


def run_pack_exhaust_stateless(
    state_root: Path,
    forge_id: str,
    *,
    max_elements: int | None = None,
    element_text_cap: int = 6000,
    model_invoke: Callable[[str, str], str] | None = None,
    model_endpoint: str | None = None,
    model_name: str = "qwen/qwen3.6-35b-a3b",
    timeout_s: float = 90.0,
    sink: str = "extractions",
) -> StatelessExhaustResult:
    """Stateless map over the manifest for a PACK forge: one grounded model
    call per pending element, each proposing pack vocabulary via the pen
    weapon's propose.* channels (+ extract.conc for entries). The set of
    alphabets declared so far is threaded into every prompt so the vocabulary
    stays consistent across elements. Requires EXHAUST phase.

    ``sink="extractions"`` (default) writes straight to the draft. ``sink=
    "candidates"`` writes to a SEPARATE pool (``stateless_candidates.jsonl``)
    that the agent reviews after its own pass — never the draft (Phase 2).
    """
    import json as _json
    state_root = Path(state_root)
    sess = ForgeSession.load(state_root, forge_id)
    if sess.phase != ForgePhase.EXHAUST:
        raise RuntimeError(
            f"forge {forge_id} is in phase {sess.phase.value}, not EXHAUST"
        )
    manifest_path = fstate.forge_manifest_path(state_root, forge_id)
    elements_by_id = {
        e.id: e for e in read_manifest(manifest_path)
        if not getattr(e, "dropped_by_operator", False)
    }
    pending = coverage_state(sess).pending
    elements = [elements_by_id[i] for i in pending if i in elements_by_id]
    if max_elements is not None:
        elements = elements[:max_elements]

    invoke = model_invoke or (
        lambda sys_p, usr_p: _stateless_model_invoke(
            sys_p, usr_p, endpoint=model_endpoint,
            model=model_name, timeout_s=timeout_s,
        )
    )

    result = StatelessExhaustResult(
        forge_id=forge_id, elements_total=len(elements))
    declared: dict[str, str] = {}  # alphabet name -> shape (threaded inventory)
    for el in elements:
        text = (getattr(el, "extracted_text", "") or "")[:element_text_cap]
        inventory = "; ".join(
            f"{n} ({s})" for n, s in declared.items()) or "(none yet)"
        user = (
            f"Alphabets so far: {inventory}\n\n"
            f"manifest_element_id: {el.id}\n\n"
            f"ELEMENT TEXT:\n{text}\n\n"
            "Output the JSON array of proposals now."
        )
        try:
            raw = invoke(_PACK_EXHAUST_SYSTEM, user)
        except Exception:  # noqa: BLE001 — a per-element failure is non-fatal
            raw = ""
        for item in _parse_json_array(raw):
            if not isinstance(item, dict):
                continue
            channel = _pack_kind_to_channel(item.get("kind"))
            body = item.get("body")
            if channel is None or not isinstance(body, dict):
                continue
            if sink == "candidates":
                # separate pool — never the draft; the agent reviews these later
                cpath = fstate.forge_candidates_path(state_root, forge_id)
                cpath.parent.mkdir(parents=True, exist_ok=True)
                with cpath.open("a") as f:
                    f.write(_json.dumps({
                        "manifest_element_id": el.id,
                        "channel": channel, "body": body,
                    }) + "\n")
                result.extractions_added += 1
                if channel == "propose.alphabet":
                    nm = str(body.get("name") or "").strip()
                    if nm:
                        declared[nm] = str(body.get("shape") or "categorical")
                continue
            try:
                record_extraction(
                    sess,
                    manifest_element_id=el.id,
                    channel=channel,
                    body=body,
                    extraction_id="px_" + uuid4().hex[:12],
                )
                result.extractions_added += 1
                if channel == "propose.alphabet":
                    nm = str(body.get("name") or "").strip()
                    if nm:
                        declared[nm] = str(body.get("shape") or "categorical")
            except ValueError:
                continue  # grounding/channel rejection — skip
        result.elements_examined += 1

    result.final_coverage = coverage_state(sess).fraction
    result.stopped_reason = "all_elements_examined"
    return result


__all__ = __all__ + ["run_pack_exhaust_stateless"]


_PACK_REVIEW_PROMPT_TEMPLATE = """\
You are Codex with the Pen equipped, inside ARA. You already forged this pack.
A separate, LOWER-QUALITY heuristic pass produced the candidate proposals below.
Your job is to REVIEW them against the pack you built.

ALPHABETS ALREADY IN THIS PACK (do not re-add what's here):
{pack_state}

CANDIDATES (from the heuristic pass — element id, channel, body):
{candidate_block}

For EACH candidate:
- KEEP: if it is a genuine addition your pack is MISSING, emit it via your
  channels (propose.alphabet / extract.conc / propose.binding), citing the same
  manifest_element_id — improving the wording/name to fit the pack if needed.
- SKIP: if it is junk, a duplicate, already covered, or worse than what you
  have, emit nothing for it.
Do NOT re-emit things already in the pack. A heuristic pass adds junk — expect
to skip most candidates. Every emit needs forge_id="{forge_id}" and the exact
manifest_element_id from the candidate. When done, call exhaust.status with
batch_complete=true.

No prose preamble. Begin tool-calling immediately.
"""


def run_pack_review(
    state_root: Path,
    forge_id: str,
    *,
    agent_invoke: Optional[Callable[[Path, str], int]] = None,
    batch_size: int = 25,
    iteration_timeout_s: float = 600.0,
) -> dict:
    """Phase 2: the contained agent reviews the stateless candidate pool AFTER
    its own forge, keeping only genuine additions (re-emitting them as real
    extractions) and skipping the rest. Candidates never touch the draft
    directly. Returns ``{"candidates": n, "kept": n}``."""
    import json as _json
    state_root = Path(state_root)
    cpath = fstate.forge_candidates_path(state_root, forge_id)
    if not cpath.exists():
        return {"candidates": 0, "kept": 0}
    candidates = [
        _json.loads(ln) for ln in cpath.read_text().splitlines() if ln.strip()
    ]
    if not candidates:
        return {"candidates": 0, "kept": 0}
    if agent_invoke is None:
        agent_invoke = lambda sr, pr: _default_agent_invoke(  # noqa: E731
            sr, pr, timeout_s=iteration_timeout_s,
            extra_env={"ARA_FORGE_MCP_MODE": "pack"},
        )
    before = _read_extractions_count(state_root, forge_id)
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        pack_state = _pack_state_primer(state_root, forge_id).get("pack_state", "")
        cand_block = "\n".join(
            f"- element {c.get('manifest_element_id')}: {c.get('channel')} "
            f"{_json.dumps(c.get('body'))[:160]}"
            for c in batch
        )
        prompt = _PACK_REVIEW_PROMPT_TEMPLATE.format(
            forge_id=forge_id, pack_state=pack_state, candidate_block=cand_block)
        try:
            agent_invoke(state_root, prompt)
        except Exception:  # noqa: BLE001 — a batch failure is non-fatal
            pass
    kept = _read_extractions_count(state_root, forge_id) - before
    return {"candidates": len(candidates), "kept": kept}


__all__ = __all__ + ["run_pack_review"]
