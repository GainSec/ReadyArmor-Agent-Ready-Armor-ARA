# Cognitive mechanisms and structured work

ARA uses explicit representations for procedure, knowledge, evidence, and review state.
These representations support practical mechanisms often associated with memory, attention, and controlled reasoning.
The mechanisms operate through files, schemas, indexes, channels, and checks.

The ten connections below explain the design in engineering terms.
They also identify which runtime or operator action activates the mechanism.

## 1. Anchored knowledge

A manifest element identifies a specific source unit.
An extraction event cites that identifier.
Synthesis carries the citation and extraction identity into the corresponding component.
Seal checks citations on its designated component groups.

Runtime facts use a related pattern.
A fact must cite evidence or depend on existing facts.
Its context includes the current activation.
The result is a record with an origin that can be examined later.

This is useful when the operator needs to distinguish source-derived knowledge from a model's unsupported suggestion.
It also makes a later correction more specific.
The operator can find the component, extraction, or evidence record involved.

Implementation: [deposit_mat](../src/ara/forge/phases/deposit.py#L45), [run_manifest](../src/ara/forge/phases/manifest.py#L27), [make_element_id](../src/ara/forge/manifest/element.py#L61), [run_exhaust](../src/ara/forge/loop_driver.py#L292), [run_exhaust_stateless](../src/ara/forge/loop_driver.py#L1017), [record_extraction](../src/ara/forge/phases/exhaust.py#L65), [seal](../src/ara/forge/phases/seal.py#L266), [validate_manifest_coverage](../src/ara/forge/phases/seal.py#L107), [validate_citation_integrity](../src/ara/forge/phases/seal.py#L119), [FactAssertChannel](../src/ara/runtime/facts/channels.py#L68), [enrich_evidence](../src/ara/runtime/provenance.py#L84), [NeighborIndex](../src/ara/runtime/facts/neighbors.py#L29).

## 2. Long-term retrieval

A sealed weapon receives a SQLite FTS5 index over its indexed components.
The tokenizer uses Porter stemming and Unicode-aware word handling.
The query path can expand configured synonyms before the FTS search.
Results carry component identities, kinds, summaries, and scores.

Calibration measures this retrieval path against operator-authored expected results.
Each query can specify expected component identities and an item kind.
The report includes precision at five results and recall at ten results.
The command returns a failing status when the configured thresholds fail.

This is an explicit verification step against a sealed weapon.
Seal copies an available calibration query file into the artifact.
The operator runs calibration before selecting that weapon for work.
The default dispatcher uses the keyword index and synonym path.

Implementation: [FTSIndex](../src/ara/forge/index/builder.py#L51), [search](../src/ara/forge/index/dispatcher.py#L55), [_cmd_calibrate](../src/ara/weapon_cli.py#L121), [run_calibration](../src/ara/forge/index/calibration.py#L70).

## 3. Typed vocabulary

A Pack declares alphabet shapes and their fields.
Categorical entries define named alternatives.
Matrices define values at explicit coordinates.
Template sets define structured reusable forms.
Vectors define numeric dimensions and permitted ranges.

Projection transforms extraction rows into these structures.
Conflicting entries become explicit conflicts.
Foreign-key checks examine relationships between alphabets.
Finding handling can then load the relevant vocabulary, rubric, templates, and impact axes from the weapon.

This reduces ambiguity between a method's terms and its output structure.
For example, a severity rubric can connect an assessment label to a defined coordinate and score.
Draft handling can preserve novel categories through the available normalization path.
The actual acceptance rules remain defined by the relevant validator.

Implementation: [AlphabetPack](../src/ara/forge/alphabets/pack.py#L131), [build_pack_draft](../src/ara/forge/pack_draft.py#L182), [seal_pack](../src/ara/forge/phases/seal.py#L230), [synthesize](../src/ara/forge/phases/synthesize.py#L223), [project](../src/ara/forge/alphabets/projector.py#L255), [FindingFileChannel](../src/ara/runtime/findings/channels.py#L333), [_rule_claim_verification](../src/ara/runtime/findings/validator.py#L367), [record_finding_fields](../src/ara/runtime/findings/field_store.py#L75).

## 4. Bounded procedure chunks

A chunk is a procedure phase with explicit context and allowed transitions.
It has a prompt fragment, a channel surface, exits, citations, and optional gems.
The runtime mounts an entry chunk and records its activation.

A worker requests a transition through `chunks.emit_tag`.
The runtime checks the tag, destination, and required caller.
A valid transition mounts the new chunk and appends its history.
Operator override tags provide pause, resume, kill, jump, and rewind controls with caller checks.

This gives the procedure a persistent position.
A natural-language statement that the phase is complete does not replace the transition record.
The supervisor also checks the active chunk's channel surface before dispatch.

Implementation: [emit_tag](../src/ara/runtime/chunks/runtime.py#L202), [_on_channel_call](../src/ara/supervisor.py#L1549), [mint](../src/ara/runtime/activation.py#L56), [record_action](../src/ara/task_session.py#L173), [record_commit](../src/ara/task_session.py#L273), [current_loadout_matches](../src/ara/task_session.py#L415).

## 5. Selective attention through inhibition

A chunk's gems associate policy layers with inhibition levels and scope settings.
An absent or malformed setting resolves to the strict profile.
The current profile can be written to session state for inspection.

With inhibition enforcement enabled, the supervisor can omit explicitly relaxable rules from a guard payload.
Scope-marked rules remain in that payload path.
The proxy has a separate decision for reply-denial relaxation under the active profile.

This supports a phase distinction between observation, active investigation, and write-up.
The selected chunk supplies the intended profile.
The configuration and enforcement path determine which adjustments apply.

The profile is a control input with visible state.
It is not a request for the model to infer its own authority.

Implementation: [resolve_inhibition_profile](../src/ara/runtime/chunks/inhibition.py#L50), [_apply_inhibition_to_payload](../src/ara/supervisor.py#L452), [_reply_deny_relaxed_by_inhibition](../src/ara/mitm_proxy.py#L1358).

## 6. Knowledge-gap consultation

A procedure can declare the fact shapes it expects to produce.
The knowledge-gap function checks whether every declared requirement already has enough facts.
Complete coverage produces a skip decision.
Partial coverage produces a fire decision with the missing requirements.

The `crumb.intent` channel can apply this check and mark the active crumb.
A one-time operator token can force work despite existing knowledge.
The function therefore separates the presence of sufficient facts from a decision to repeat the procedure.

This is an explicit integration surface.
An integration installs the channel with an engagement fact store and calls it before the procedure.
The generic default engagement installation does not automatically schedule procedures from this API.

Implementation: [CrumbIntentChannel](../src/ara/runtime/chunks/crumb_intent.py#L58), [has_sufficient_coverage](../src/ara/runtime/knowledge_gap.py#L58), [install_engagement](../src/ara/runtime/bootstrap.py#L175).

## 7. Activation freshness

An activation identifies a current execution context.
The activation manager records a new identity at initialization and chunk transitions.
Evidence captures can record that identity.
Evidence access through the relevant channels can also contribute freshness information.

Finding freshness checks can require evidence captured or accessed during the current activation.
This gives the validator more information than the mere existence of an old artifact.
The relevant channel context and freshness setting control the check.

This is useful when a later phase must establish its result against current observations.
It connects procedure position to evidence use.

Implementation: [emit_tag](../src/ara/runtime/chunks/runtime.py#L202), [_on_channel_call](../src/ara/supervisor.py#L1549), [mint](../src/ara/runtime/activation.py#L56), [put](../src/ara/runtime/binder/api.py#L44), [capture_tool_exchanges](../src/ara/runtime/binder/tool_capture.py#L108), [_capture_evidence](../src/ara/mitm_proxy.py#L2396), [FindingFileChannel](../src/ara/runtime/findings/channels.py#L333), [_rule_claim_verification](../src/ara/runtime/findings/validator.py#L367), [record_finding_fields](../src/ara/runtime/findings/field_store.py#L75).

## 8. Expected-shape comparison

A procedure can carry an expected evidence shape.
The anomaly subsystem compares captured content with that shape.
It can examine required fields, patterns, unexpected fields, structure, lines, and sections.
The result is a divergence vector and aggregate score.

The capture hook consults the active crumb and loaded weapon shapes.
If the necessary context exists, the hook records divergence telemetry.
A score at or above the threshold can call `anomaly.emit`.
The operator can then promote or dismiss the anomaly.

This gives unexpected observations their own lifecycle.
An anomaly identifies a difference that needs interpretation.
It does not itself establish a confirmed vulnerability.

Implementation: [on_evidence](../src/ara/runtime/anomaly/autofire.py#L79), [compute_divergence](../src/ara/runtime/anomaly/divergence.py#L30), [put](../src/ara/runtime/binder/api.py#L44), [capture_tool_exchanges](../src/ara/runtime/binder/tool_capture.py#L108), [_capture_evidence](../src/ara/mitm_proxy.py#L2396).

## 9. Priority and retrievability

Lead priority is calculated from structured signals.
The available signals include evidence strength, evidence diversity, value tier, procedure provenance, scope weight, and optional prior yield.
An operator override remains distinct from computed priority.

Lead retrievability follows an exponential decay function:

```text
R(t) = exp(-elapsed_time / stability)
```

Review actions change the lead's state and stability.
This gives unresolved work an explicit age and review history.
It supports a distinction between a high-value unresolved question and a recently examined one.

The closure-check API can identify stale high-priority leads.
A workflow must call that API to apply its closure decision.
Cross-engagement yield is also an explicit input to the priority function.

Implementation: [compute_priority](../src/ara/runtime/leads/priority.py#L57), [retrievability](../src/ara/runtime/leads/decay.py#L53), [check_closure](../src/ara/runtime/leads/closure_gate.py#L26).

## 10. Typed communication and selective retention

Typed channels separate reading evidence from asserting a fact, filing a lead, recording a finding, and changing phase.
Caller checks and body schemas apply before the operation.
The channel result changes a specific store.
This preserves the difference between observation, interpretation, and lifecycle control.

Selective retention also applies during authoring.
The operator can drop irrelevant manifest elements without losing the exclusion record.
Those elements stop contributing to the extraction coverage denominator.
Reforge later records a typed comparison with a predecessor weapon.

Style profiles provide another explicit output interface.
An installed `style.score` channel can compare text with a weapon style profile in permitted chunks.
Style scoring is an optional channel registration.
The main report path also returns section diagnostics independently of that registration.

These mechanisms make reuse and omission deliberate operations.
The operator can retain useful structure, remove irrelevant material, and review changes before future use.

Implementation: [_on_query](../src/ara/supervisor.py#L781), [call](../src/ara/runtime/channels/base.py#L90), [_merge_with_prior_drops](../src/ara/forge/phases/manifest.py#L70), [record_extraction](../src/ara/forge/phases/exhaust.py#L65), [_start_polish_task](../src/ara/armory/app.py#L1025), [build_directive](../src/ara/armory/polish.py#L154), [propose](../src/ara/forge/reforge/driver.py#L55), [ReportRenderChannel](../src/ara/runtime/report/channels.py#L25), [render_report](../src/ara/runtime/report/kiln.py#L119), [StyleScoreChannel](../src/ara/runtime/style/channels.py#L21).

## A worked evidence check

Suppose a finding claims that a page is available without authentication.
The evidence store contains the captured reply for that page.
The finding supplies an explicit required marker and an excluded login marker.

```json
{
  "verification": {
    "claim": "The selected page returns the account summary without authentication.",
    "must_contain": ["Account summary"],
    "must_not_contain": ["Sign in to continue"]
  },
  "evidence_refs": ["e:CAPTURE_ID"]
}
```

The validator reads the bytes referenced by `evidence_refs`.
An excluded marker rejects the claim.
A missing explicit required marker also rejects the claim.
The operator can inspect the same captured content during review.

This check establishes the configured content conditions.
Impact, remediation, scope, and final acceptance still have their own review requirements.
A complete result therefore combines content checks with the operator's assessment of the evidence.

Implementation: [FindingFileChannel](../src/ara/runtime/findings/channels.py#L333), [_rule_claim_verification](../src/ara/runtime/findings/validator.py#L367), [record_finding_fields](../src/ara/runtime/findings/field_store.py#L75), [FindingSignOffChannel](../src/ara/runtime/findings/channels.py#L486), [validate_for_signoff](../src/ara/runtime/findings/validator.py#L662).
