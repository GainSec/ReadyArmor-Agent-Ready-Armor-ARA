# ARA system codex

This codex explains the named parts of ARA and their working relationships.
Read it from source preparation to accepted output, or use the component sections as a reference.

## The connected system

```text
KEEP materials → Sew engagement context → Trim
         ↓
Forge manifest → extraction → Pack or Weapon → reviewed seal
                                               ↓
Armor + Weapon + Trim + Temper → preflight → supervised task
                                               ↓
operation gates + model mediation + chunk channels
                                               ↓
Evidence Binder → facts / leads / anomalies / findings
                                               ↓
Spoils review → operator sign-off → Report Kiln
                                               ↓
focused work / baseline / hardening / Reforge
```

Each arrow carries an explicit record or state change.
Source material, operating permission, captured evidence, and accepted claims retain distinct identities.

## Armory: the operator control surface

Armory organizes the complete lifecycle around the current state directory.
It exposes equipment, source materials, Forge, policy, task activity, and review.
Administrator access permits mutations.
Viewer access provides observation through the supported viewing interface.

The server handler applies authentication before protected actions.
Actions call the same underlying modules used by the CLI.
The browser views read persistent state to show the resulting condition.

Armory also starts and stops supervised tasks.
The task launcher records the selected equipment, command, task identifier, process identifier, and log path.
This makes a task start a reviewable state change rather than an isolated browser request.

Implementation: [check_active_trim_for_task](../src/ara/task_preflight.py#L25), [_start_task_session](../src/ara/armory/app.py#L1084), [_build_durable_hermes_worker](../src/ara/armory/app.py#L403), [repair_commit_debt](../src/ara/task_repair.py#L40).

## KEEP and Mats: source ownership

A Mat is an input material.
KEEP is the operator workspace for materials associated with an engagement.
The storage classes separate source, architecture, API specifications, documents, artifacts, and credentials.

A material record contains its path, class, SHA-256 hash, size, upload time, uploader, tags, and note.
Selection is represented in the manifest.
An excluded item remains inventoried while selected-class calculations omit it.
Credential directories use tighter permissions than ordinary material directories.

This separation matters during engagement preparation.
The chosen posture and available material classes inform the generated context.
A Forge deposit can also be associated with an engagement, reducing duplicate material preparation.

Implementation: [ensure_engagement](../src/ara/engagement_mats.py#L125), [add_mat_bytes](../src/ara/engagement_mats.py#L235), [set_mat_selected](../src/ara/engagement_mats.py#L347), [deposit_mat](../src/ara/forge/phases/deposit.py#L45), [run_manifest](../src/ara/forge/phases/manifest.py#L27), [make_element_id](../src/ara/forge/manifest/element.py#L61).

## Sew: turn engagement intent into context

Sew is the Trim derivation interface.
It collects the engagement identifier, target, scope, operating environment, activity posture, and review preferences.
It also captures operating windows, authorizations, channels, cadence, and stopping criteria where selected.

The form parser produces a structured engagement record.
`derive_trim` writes canonical intake and the resulting Trim files.
The generated policy and instruction fragments carry that context into the active loadout.
Applying the Trim adds its marked policy sections.
Releasing it removes those sections.

The generated Trim is reusable within its intended engagement context.
Its concrete targets and times remain separate from the standing armor identity.

Implementation: [parse_derive_form](../src/ara/armory/trim_views.py#L573), [derive_trim](../src/ara/trims.py#L806), [apply_trim](../src/ara/trims.py#L643).

## Armor: persistent identity

An armor groups policy scope, metadata, instruction variants, and operating settings.
Its ABSOLUTE variants provide selected instruction text.
Its resource rules define standing boundaries for the identity.

The active armor marker is read by the launcher, policy scope logic, request composer, and operator views.
Changing the selected armor also attempts to release the prior armor's active Trim.
This prevents a previous engagement overlay from silently becoming the new identity's context.

Clone, export, and import operations support deliberate reuse of an identity package.
The [equipment guide](equipment.md) describes everyday selection and release operations.

Implementation: [set_active_armor](../src/ara/armors.py#L371), [load_armor](../src/ara/armors.py#L436).

## Weapon: sealed method

A weapon is the reviewed output of capability creation.
It can carry procedure crumbs, knowledge shapes, fact schemas, stores, resources, scope lines, initial facts, and policy material.
It also carries chunks, projected alphabets, a channel catalog, a search index, and seal metadata where generated.

Equipment adds the weapon's policy as marked sections in the current policy files.
Unequipment removes those sections.
This preserves the operator's standing content outside the markers.

The supervisor mounts the weapon's runtime channels and chunk graph during engagement installation.
An equipment marker change and a channel graph replacement are distinct operations.
Task loadout hashes provide an additional continuity check during an active run.

Implementation: [equip_policy](../src/ara/weapons.py#L209), [unequip_policy](../src/ara/weapons.py#L252), [_maybe_install_engagement](../src/ara/supervisor.py#L1514), [seal](../src/ara/forge/phases/seal.py#L266), [validate_manifest_coverage](../src/ara/forge/phases/seal.py#L107), [validate_citation_integrity](../src/ara/forge/phases/seal.py#L119), [record_action](../src/ara/task_session.py#L173), [record_commit](../src/ara/task_session.py#L273), [current_loadout_matches](../src/ara/task_session.py#L415).

## Pack and alphabet: domain structure

A Pack defines the vocabulary and workflow material used during capability creation.
It includes alphabet declarations, validator bindings, detection hints, chunk templates, and policy defaults.
Built-in Packs and operator-created runtime Packs share the resolution interface.
A runtime Pack can supply the selected definition by name.

An alphabet is a typed collection with an explicit shape:

| Shape | Structure | Example use |
|---|---|---|
| `categorical` | Entries with declared fields | Finding categories |
| `matrix` | Values at named coordinates | Severity rubric |
| `template_set` | Named structured templates | Remediation patterns |
| `vector` | Named numeric axes and ranges | Impact dimensions |

Projection routes extraction rows into those shapes.
Conflict and foreign-key checks preserve consistency between linked entries.
The resulting files can supply category, rubric, fix, and impact information to finding handling.

A Pack is selected while a capability is created.
Its materialized weapon data reaches the runtime through the weapon artifact.

Implementation: [AlphabetPack](../src/ara/forge/alphabets/pack.py#L131), [build_pack_draft](../src/ara/forge/pack_draft.py#L182), [seal_pack](../src/ara/forge/phases/seal.py#L230), [synthesize](../src/ara/forge/phases/synthesize.py#L223), [project](../src/ara/forge/alphabets/projector.py#L255), [detect_conflict_groups](../src/ara/forge/reconcile.py#L129), [apply_decision](../src/ara/forge/reconcile.py#L218).

## Forge: capability construction

Forge records the phases of source preparation, extraction, synthesis, review, and seal.
The repeated review stage lets the operator inspect different artifacts before the next transformation.

Manifest elements identify the source units selected for extraction.
Typed extraction channels preserve each output's source element identifier.
Coverage connects extraction events to the included manifest.
Synthesis organizes the events into a draft.
Seal applies its validation rules and writes the equipment artifact.

Forge supports several authoring paths.
Heuristic extractors handle supported structured material.
Contained agent loops process material through the current MCP surface.
Stateless model paths provide another structured extraction route.
The operator reviews the resulting artifacts regardless of the chosen path.

The [Forge guide](forge.md) gives the command sequence.
The [end-to-end guide](end-to-end.md) explains how that output becomes a live run.

Implementation: [deposit_mat](../src/ara/forge/phases/deposit.py#L45), [run_manifest](../src/ara/forge/phases/manifest.py#L27), [make_element_id](../src/ara/forge/manifest/element.py#L61), [run_exhaust](../src/ara/forge/loop_driver.py#L292), [run_exhaust_stateless](../src/ara/forge/loop_driver.py#L1017), [record_extraction](../src/ara/forge/phases/exhaust.py#L65), [synthesize](../src/ara/forge/phases/synthesize.py#L223), [project](../src/ara/forge/alphabets/projector.py#L255), [seal](../src/ara/forge/phases/seal.py#L266), [validate_manifest_coverage](../src/ara/forge/phases/seal.py#L107), [validate_citation_integrity](../src/ara/forge/phases/seal.py#L119).

## Blacksmith, Codex, Hammer, and Pen

Capability creation uses the same equipment model as other work.
Blacksmith armor and the Blacksmith Hammer provide weapon-authoring restrictions and extraction tools.
Codex armor and the Pen provide Pack-authoring vocabulary tools.

Pack extraction, reconciliation, and workflow derivation expose different MCP tool sets.
A reconciliation job can merge or split existing vocabulary and resolve a cell.
A workflow job derives chunk nodes through its specific interface.
The active job therefore controls the available authoring operations.

This is a practical example of reusable authority and capability composition.
The authoring worker receives a job-specific surface while the operator retains review and seal control.

Implementation: [run_exhaust](../src/ara/forge/loop_driver.py#L292), [run_exhaust_stateless](../src/ara/forge/loop_driver.py#L1017), [record_extraction](../src/ara/forge/phases/exhaust.py#L65), [AlphabetPack](../src/ara/forge/alphabets/pack.py#L131), [build_pack_draft](../src/ara/forge/pack_draft.py#L182), [seal_pack](../src/ara/forge/phases/seal.py#L230), [detect_conflict_groups](../src/ara/forge/reconcile.py#L129), [apply_decision](../src/ara/forge/reconcile.py#L218).

## Trim and Temper: engagement context and hardening

Trim supplies one engagement's concrete context.
Temper supplies reusable hardening that can apply across task methods.
The current first-class Temper store has independent policy files and an equipped stack.
ABSOLUTE composition reads doctrine from that stack.

Armory's current Temper creation path installs curated baseline hardening.
Operators can also ingest selected reviewed proposal entries into a first-class package.
The older armor-scoped proposal representation remains available for its apply, activate, release, and task projection operations.

Tailor and Needle provide source-derived candidate machinery.
Candidates have a destination-specific control object, citations, rationale, risk, and review decision.
Coverage records account for each selected source and destination.
These programmatic authoring interfaces are separate from the current curated Armory creation page.

Implementation: [handle_temper_create](../src/ara/armory/temper_views.py#L498), [handle_temper_tailor_start](../src/ara/armory/temper_views.py#L549), [ingest](../src/ara/tempers.py#L118), [equip](../src/ara/tempers.py#L247), [call_tool](../src/ara/clients/temper_mcp_server.py#L514), [_validate_tailor_candidate](../src/ara/temper_lifecycle.py#L195), [add_tailor_coverage_summary](../src/ara/temper_lifecycle.py#L594), [validate_control](../src/ara/temper_controls.py#L153).

## Bulwark: resource policy

Bulwark parses one live policy file into scoped rules.
Each rule selects an action, layer, match expression, and optional modifiers.
Global and armor scopes determine where the rule applies.
Deny and approval groups precede allow groups in the deterministic evaluator.

The resource layers cover files, directories, permissions, executables, network connections, URLs, environment variables, and supported worker tool targets.
Model request and reply layers use the same policy store through their applicable mediation path.
Time and hit-count modifiers can bound a rule's use.

The [policy reference](BULWARK-SPEC.md) gives the exact grammar and precedence.
The supervisor records the final decision after its additional applicable gates.

Implementation: [_on_query](../src/ara/supervisor.py#L781), [evaluate](../src/ara/bulwark.py#L820), [evaluate_call](../src/ara/runtime/trim_gate.py#L109), [evaluate_gate](../src/ara/runtime/tod.py#L376), [acquire](../src/ara/runtime/rate_bucket.py#L223).

## Resource consumers and Control in Depth

Bulwark policy reaches actual operations through specific consumers.
A loaded native shim can return `EACCES` before a matching file, executable, or connection operation.
Node hooks apply local rules or submit supported asynchronous queries.
The Python task bootstrap queries before the original `Popen` call.
An installed Linux seccomp filter mediates selected network syscalls through its notification listener.

The full evaluator, local projections, and network connection paths have distinct contracts.
General queries use deny-first precedence and permit unmatched events.
The connection paths use allow-first matching.
Native command and network projections omit regex rules and live modifier state.
The native cache comes from the initial handshake; Node can receive updates.

Armory selects Hermes by default and launches its normal task through the contained persistent loop.
The generic launcher starts a supplied executable.
Claude Code and OpenClaw are explicit adapter choices.
The current host, actual guard loading, configuration, and selected observation mode determine the active coverage.

The [Control in Depth guide](control-in-depth.md) gives the blocking branches, failure behavior, and worked out-of-scope attempts.
It also distinguishes operation denials, task obligations, model guidance, observation, record acceptance, and operator authority.

## Rhema, ABSOLUTE, and Shadows

Rhema provides the model traffic view and related policy controls.
ABSOLUTE supplies the selected system instruction composition.
The composer reads armor instructions, configured default sections, task projections, and equipped Temper doctrine where applicable.

The proxy applies that composition to supported model envelopes.
It also evaluates applicable request and reply decisions and captures interactions.
A shadow variant can produce an alternate request and reply for comparison.
Promotion is an explicit operator action.

Armor shadow mode has a different purpose.
It records selected would-deny operation outcomes while permitting those operations.
Keep that observation setting distinct from live enforcement and model-request comparison.

Implementation: [active_absolute_rule](../src/ara/_rhema_shim.py#L242), [apply_rewrite_spec](../src/ara/_rhema_shim.py#L165), [_try_apply_request_rewrite](../src/ara/mitm_proxy.py#L1594), [_dispatch_shadow](../src/ara/mitm_proxy.py#L2443), [_on_query](../src/ara/supervisor.py#L781).

## Tools, TOD, rate buckets, and stop latches

The tool registry tracks tool identity and flag patterns with classification state.
A first-use decision can request classification or operator review according to the selected posture.
The registry preserves later operator corrections.

TOD means time of day.
The engagement's operating windows use the configured timezone.
The available decision flow can extend work, move to read-only activity, defer work, or cancel it.

Rate buckets track permitted request cadence through persistent token state.
Stop latches hold work when configured criteria are met.
The Trim gate composes these mechanisms for its supported operations.

Implementation: [evaluate_call](../src/ara/runtime/trim_gate.py#L109), [evaluate_gate](../src/ara/runtime/tod.py#L376), [acquire](../src/ara/runtime/rate_bucket.py#L223), [_on_query](../src/ara/supervisor.py#L781), [_shadow_attest_danger](../src/ara/supervisor.py#L1095), [wait_for_resolution](../src/ara/approvals.py#L265).

## Task controller, chunks, crumbs, and gems

A task record tracks action budgets, committed records, branch progress, verification debt, and equipment continuity.
When a budget is reached, the next target operation can be held for the corresponding task obligation.
The persistent Hermes loop uses these states to continue, repair, verify, or transition.

A chunk is a bounded phase in the sealed procedure graph.
It carries a prompt fragment, channel surface, exits, citations, and entry or terminal flags.
A valid transition mounts the next chunk and creates a new activation.

A crumb is an individual procedure or intent unit.
Crumb state can identify the procedure whose expected evidence shape applies.
A gem associates a policy layer with the chunk's inhibition settings.
Actual rule relaxation is configuration-dependent and applies through its specific enforcement path.

The [cognitive mechanism guide](cognitive-mechanisms.md) explains these structures and their integration points.

Implementation: [record_action](../src/ara/task_session.py#L173), [record_commit](../src/ara/task_session.py#L273), [current_loadout_matches](../src/ara/task_session.py#L415), [_build_durable_hermes_worker](../src/ara/armory/app.py#L403), [_start_task_session](../src/ara/armory/app.py#L1084), [repair_commit_debt](../src/ara/task_repair.py#L40), [emit_tag](../src/ara/runtime/chunks/runtime.py#L202), [_on_channel_call](../src/ara/supervisor.py#L1549), [mint](../src/ara/runtime/activation.py#L56), [resolve_inhibition_profile](../src/ara/runtime/chunks/inhibition.py#L50), [_apply_inhibition_to_payload](../src/ara/supervisor.py#L452), [_reply_deny_relaxed_by_inhibition](../src/ara/mitm_proxy.py#L1358), [CrumbIntentChannel](../src/ara/runtime/chunks/crumb_intent.py#L58), [has_sufficient_coverage](../src/ara/runtime/knowledge_gap.py#L58), [install_engagement](../src/ara/runtime/bootstrap.py#L175).

## Evidence Binder, facts, leads, and anomalies

The Binder stores observed bytes and their metadata.
Facts record structured observations grounded in evidence or existing facts.
A neighborhood query can expose causal, temporal, and evidential connections around a fact.

Leads preserve unresolved questions.
Their priority uses structured signals such as evidence strength, value, provenance, and scope weight.
Their retrievability score changes with elapsed time and review history.
Operator actions can investigate, stabilize, dismiss, confirm, or override priority.

An anomaly compares an observation with a declared expected shape.
When an active crumb and shape are available, the installed capture hook can calculate divergence.
The result can remain telemetry or create an anomaly record for review.

Implementation: [put](../src/ara/runtime/binder/api.py#L44), [capture_tool_exchanges](../src/ara/runtime/binder/tool_capture.py#L108), [_capture_evidence](../src/ara/mitm_proxy.py#L2396), [FactAssertChannel](../src/ara/runtime/facts/channels.py#L68), [enrich_evidence](../src/ara/runtime/provenance.py#L84), [NeighborIndex](../src/ara/runtime/facts/neighbors.py#L29), [compute_priority](../src/ara/runtime/leads/priority.py#L57), [retrievability](../src/ara/runtime/leads/decay.py#L53), [check_closure](../src/ara/runtime/leads/closure_gate.py#L26), [on_evidence](../src/ara/runtime/anomaly/autofire.py#L79), [compute_divergence](../src/ara/runtime/anomaly/divergence.py#L30).

## Findings, Spoils, and Report Kiln

A finding holds a claim, supporting references, impact, remediation, affected assets, and lifecycle state.
Draft filing checks applicable references and claim conditions.
For mechanically checkable claims, the validator reads cited evidence bytes.
Explicit required and excluded markers provide direct content checks.
Derived marker checks use the available claim text.

Spoils presents findings, leads, facts, evidence links, and review dispositions.
The operator can edit a record, attach evidence, or launch focused work against a specific item.
Sign-off applies the stronger review requirements.

The Report Kiln reads signed-off findings and their evidence context.
It applies a weapon report template or the default template.
The result is Markdown, section diagnostics, a report record, and a content hash.

Implementation: [FindingFileChannel](../src/ara/runtime/findings/channels.py#L333), [_rule_claim_verification](../src/ara/runtime/findings/validator.py#L367), [record_finding_fields](../src/ara/runtime/findings/field_store.py#L75), [FindingSignOffChannel](../src/ara/runtime/findings/channels.py#L486), [validate_for_signoff](../src/ara/runtime/findings/validator.py#L662), [ReportRenderChannel](../src/ara/runtime/report/channels.py#L25), [render_report](../src/ara/runtime/report/kiln.py#L119), [StyleScoreChannel](../src/ara/runtime/style/channels.py#L21), [_start_polish_task](../src/ara/armory/app.py#L1025), [build_directive](../src/ara/armory/polish.py#L154), [propose](../src/ara/forge/reforge/driver.py#L55).

## Baselines, canaries, audit, and Reforge

A baseline captures the resources observed during a reference session.
Later comparisons show new or missing observed resources.
The operator can turn a baseline into an armor candidate.

A canary is a marked file or value.
Detection and the policy decision that blocks a reply are separate mechanisms.
The corresponding reply rule supplies the applicable blocking condition.

Audit chains make recorded sequences verifiable for changes.
Session export includes selected session files, the contained directory tree, and audit attestation.
A detached HMAC uses the shared administrator secret for verification.

Reforge compares a new draft with its predecessor.
Its proposal records component additions, removals, modifications, rationales, and approval information.
The operator approves the proposal before the later seal step.
This is the explicit growth path for reusable capability.

Implementation: [record_from_session](../src/ara/baseline.py#L135), [diff_against_session](../src/ara/baseline.py#L162), [to_armor](../src/ara/baseline.py#L222), [add](../src/ara/canary.py#L188), [_check_canaries_in_reply](../src/ara/mitm_proxy.py#L1244), [_try_enforce_bulwark_response](../src/ara/mitm_proxy.py#L1289), [build_session_export](../src/ara/armory/app.py#L1414), [build_session_export_signature](../src/ara/armory/app.py#L1506), [_start_polish_task](../src/ara/armory/app.py#L1025), [build_directive](../src/ara/armory/polish.py#L154), [propose](../src/ara/forge/reforge/driver.py#L55).

## Deployment and change observation

The contained Hermes gateway guard compares running gateway processes with their ARA launcher ancestry.
Its production reconciliation path can restore the configured contained service and record a circuit-breaker hold.
That path uses the host's process and user-service facilities.
Deployment configuration supplies the applicable executable and service paths.

Skill change observation fingerprints the configured skill directories.
A fresh snapshot identifies additions, removals, and modified content against the stored index.
The resulting events make those changes available for operator review.

The optional MCP scanner integration supplies another observation surface.
It can scan a client configuration or run an external scanner proxy.
The operator installs the scanner and routes the relevant MCP traffic through it.
Parsed findings enter the event stream for examination.

These mechanisms extend supervision beyond individual target operations.
They preserve visible deployment and integration changes around the worker.

Implementation: [find_uncontained_hermes](../src/ara/hermes_guard.py#L86), [run_reconcile](../src/ara/hermes_guard.py#L329), [start_hermes_guard_thread](../src/ara/armory/app.py#L1384).

Implementation: [scan](../src/ara/skill_watch.py#L246), [changes_to_events](../src/ara/skill_watch.py#L259).

Implementation: [run_static_scan](../src/ara/mcp_scan.py#L93), [launch_proxy_sidecar](../src/ara/mcp_scan.py#L117).
