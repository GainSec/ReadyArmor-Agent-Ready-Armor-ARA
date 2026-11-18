# Feature and functionality reference

Each feature below identifies the trigger, mechanism, persistent result, and operator action.
The references point to the implementation in this repository.

## Feature families

| Family | Components |
|---|---|
| Preparation | KEEP, engagement intake, Sew, equipment |
| Capability creation | Manifest, extraction, Pack projection, reconciliation, seal, retrieval, Reforge |
| Execution | Task preflight, Hermes loop, operation gates, chunks, budgets |
| Model mediation | Rhema, ABSOLUTE, request and reply rules, shadow comparison |
| Evidence and acceptance | Binder, facts, leads, anomalies, findings, sign-off, reports |
| Review and reuse | Spoils, focused work, baselines, hardening, exports |

## External authority and typed acceptance

Policy and typed output contracts are evaluated outside the worker model.

| Part | Behavior |
|---|---|
| Trigger | Worker operation or channel message |
| Mechanism | Supervisor and Channel.call |
| State | `policy_hits.jsonl, channel event stores` |
| Result | allow/deny/approval or accepted/rejected record |
| Operator action | Armory policy, approvals, Spoils |

The selected runtime path determines interception coverage. Shadow and configured approval automation determine how relevant decisions are applied.

Implementation: [_on_query](../src/ara/supervisor.py#L781), [call](../src/ara/runtime/channels/base.py#L90).

## KEEP materials

KEEP holds class-partitioned materials and canonical engagement intake.

| Part | Behavior |
|---|---|
| Trigger | Upload/paste/rescan/select |
| Mechanism | engagement_mats validators and storage |
| State | `engagements/<id>/intake.json,manifest.json,mats/` |
| Result | Hashed upload records and selected classes |
| Operator action | KEEP upload/select UI |

Credential class has tighter directory permissions; excluded files remain inventoried.

Implementation: [ensure_engagement](../src/ara/engagement_mats.py#L125), [add_mat_bytes](../src/ara/engagement_mats.py#L235), [set_mat_selected](../src/ara/engagement_mats.py#L347).

## Sew and Trim derivation

Sew derives a Trim deterministically from operator intake.

| Part | Behavior |
|---|---|
| Trigger | /trims/derive POST |
| Mechanism | derive_trim and generated Bulwark/ABSOLUTE sections |
| State | `armors/<id>/trims/<trim>/meta.json,bulwark-section.md,absolute-trim.md` |
| Result | Task context and marked policy overlay |
| Operator action | Sew form and Trim detail/apply |

Targets, authorization, windows and task cadence belong to Trim.

Implementation: [parse_derive_form](../src/ara/armory/trim_views.py#L573), [derive_trim](../src/ara/trims.py#L806), [apply_trim](../src/ara/trims.py#L643).

## Armor identity

Armor selects persistent identity, doctrine and scoped policy.

| Part | Behavior |
|---|---|
| Trigger | armor equip or Armory selection |
| Mechanism | active armor helpers and policy scope |
| State | `active-armor,armors/<name>/` |
| Result | Selected identity and ABSOLUTE variant |
| Operator action | Armor details/clone/import/export |

Changing armor releases prior active Trim best effort.

Implementation: [set_active_armor](../src/ara/armors.py#L371), [load_armor](../src/ara/armors.py#L436).

## Weapon equipment

Weapon equipment merges reversible policy overlays.

| Part | Behavior |
|---|---|
| Trigger | weapon equip |
| Mechanism | equip_policy and unequip_policy |
| State | `equipped_weapon,marked live policy blocks` |
| Result | Standing policy preserved outside marked blocks |
| Operator action | Weapon detail and calibration |

Runtime graph is installed at supervisor startup; marker reload is distinct from graph remount.

Implementation: [equip_policy](../src/ara/weapons.py#L209), [unequip_policy](../src/ara/weapons.py#L252), [_maybe_install_engagement](../src/ara/supervisor.py#L1514).

## Curated Temper hardening

Current Armory creates curated first-class Temper packages.

| Part | Behavior |
|---|---|
| Trigger | Temper create/install baseline or selected proposal entries |
| Mechanism | install_baseline_temper,ingest,equip |
| State | `tempers/<id>/policy,active-tempers` |
| Result | Stacked hardening and live doctrine composition |
| Operator action | Temper list/selected review/equip |

Armory uses the curated creation page. The Tailor and Needle lifecycle APIs provide a separate programmatic authoring path.

Implementation: [handle_temper_create](../src/ara/armory/temper_views.py#L498), [handle_temper_tailor_start](../src/ara/armory/temper_views.py#L549), [ingest](../src/ara/tempers.py#L118), [equip](../src/ara/tempers.py#L247).

## Tailor and Needle candidate interfaces

Tailor/Needle validates source-derived candidate schemas before promotion.

| Part | Behavior |
|---|---|
| Trigger | Temper MCP tools |
| Mechanism | validate_control, candidate/citation/coverage validation |
| State | `temper/tailor-session.json,proposals` |
| Result | Accepted candidate or structured rejection |
| Operator action | Candidate selection, seal and ingest |

Programmatic lifecycle; all seven destinations and source coverage accounted.

Implementation: [call_tool](../src/ara/clients/temper_mcp_server.py#L514), [_validate_tailor_candidate](../src/ara/temper_lifecycle.py#L195), [add_tailor_coverage_summary](../src/ara/temper_lifecycle.py#L594), [validate_control](../src/ara/temper_controls.py#L153).

## Multi-format source manifest

Forge deposits become stable source manifest elements.

| Part | Behavior |
|---|---|
| Trigger | File deposit and manifest operation |
| Mechanism | deposit_mat,enumerate/read extractors |
| State | `forge/<id>/mats,manifest.jsonl` |
| Result | Element IDs, hashes, text, ranges, format metadata |
| Operator action | Drop/restore/approve manifest |

Optional PDF/Word/Excel/OCR libraries and Tesseract necessary for their formats.

Implementation: [deposit_mat](../src/ara/forge/phases/deposit.py#L45), [run_manifest](../src/ara/forge/phases/manifest.py#L27), [make_element_id](../src/ara/forge/manifest/element.py#L61).

## Selective source retention

Operator exclusions survive fresh manifest enumeration.

| Part | Behavior |
|---|---|
| Trigger | Drop or restore element |
| Mechanism | _merge_with_prior_drops and record_extraction |
| State | `dropped_by_operator flags,audit` |
| Result | Excluded elements removed from coverage denominator and extraction citation set |
| Operator action | Manifest review |

IDs include content hash and material relative path; changes can produce new IDs.

Implementation: [_merge_with_prior_drops](../src/ara/forge/phases/manifest.py#L70), [record_extraction](../src/ara/forge/phases/exhaust.py#L65).

## Typed extraction paths

Contained or stateless extraction writes typed source-cited events.

| Part | Behavior |
|---|---|
| Trigger | run-exhaust or Armory extraction |
| Mechanism | Forge MCP schema/channels/record_extraction |
| State | `extractions.jsonl,turn-status,coverage` |
| Result | Structured extraction body with manifest citation |
| Operator action | Coverage and draft review |

Model-assisted extraction is not globally deterministic; heuristic auto-extractor is a separate path.

Implementation: [run_exhaust](../src/ara/forge/loop_driver.py#L292), [run_exhaust_stateless](../src/ara/forge/loop_driver.py#L1017), [record_extraction](../src/ara/forge/phases/exhaust.py#L65).

## Weapon synthesis

Synthesis groups extraction events and projects pack vocabularies.

| Part | Behavior |
|---|---|
| Trigger | Synthesize |
| Mechanism | synthesize,project,foreign-key checks |
| State | `weapon_draft.json,alphabets summary,policy text` |
| Result | Reviewable weapon candidate |
| Operator action | Draft and conflict review |

Conflict can block synthesis; pack defaults supply chunks/policy only where absent.

Implementation: [synthesize](../src/ara/forge/phases/synthesize.py#L223), [project](../src/ara/forge/alphabets/projector.py#L255).

## Domain Packs and alphabet shapes

Pack Forge produces reusable alphabet/workflow/policy definitions.

| Part | Behavior |
|---|---|
| Trigger | Forge kind=pack |
| Mechanism | pack mode channel restrictions,draft curation,seal_pack |
| State | `pack_draft.json,runtime-packs/<name>.json` |
| Result | Reusable pack with categorical/matrix/template_set/vector shapes |
| Operator action | Toggle/add/edit curated entries,seal |

Pack is a Forge input and materialized artifact, not a live active-pack slot.

Implementation: [AlphabetPack](../src/ara/forge/alphabets/pack.py#L131), [build_pack_draft](../src/ara/forge/pack_draft.py#L182), [seal_pack](../src/ara/forge/phases/seal.py#L230).

## Explicit conflict handling

Pack conflicts have explicit merge,split,cell-resolution and drop operations.

| Part | Behavior |
|---|---|
| Trigger | Reconcile tools |
| Mechanism | apply_decision and conflict detection |
| State | `pack_draft,decision ledger` |
| Result | Operator/agent-mediated draft changes |
| Operator action | Pack review |

Conflicts use explicit draft operations and subsequent review.

Implementation: [detect_conflict_groups](../src/ara/forge/reconcile.py#L129), [apply_decision](../src/ara/forge/reconcile.py#L218).

## Validated equipment seal

Seal validates and writes executable equipment artifacts.

| Part | Behavior |
|---|---|
| Trigger | Operator seal request |
| Mechanism | Target ID,coverage,citation and alphabet validators |
| State | `weapon.json,chunks,alphabets,policy,catalog,search/fts.db,seal.json` |
| Result | Hash-stamped weapon and Forge audit |
| Operator action | Seal failures return session to REVIEW |

The force option can accept soft coverage or empty-alphabet conditions. Citation and target identity checks continue to apply.

Implementation: [seal](../src/ara/forge/phases/seal.py#L266), [validate_manifest_coverage](../src/ara/forge/phases/seal.py#L107), [validate_citation_integrity](../src/ara/forge/phases/seal.py#L119).

## Retrieval calibration

Calibration is an explicit retrieval verification command.

| Part | Behavior |
|---|---|
| Trigger | ara weapon calibrate |
| Mechanism | run_calibration over SearchDispatcher |
| State | `calibration_queries.json,calibration_report.json` |
| Result | Precision@5,recall@10 and threshold report |
| Operator action | Operator examines retrieval quality |

The operator runs calibration explicitly. Seal copies an available query file into the weapon.

Implementation: [_cmd_calibrate](../src/ara/weapon_cli.py#L121), [run_calibration](../src/ara/forge/index/calibration.py#L70), [search](../src/ara/forge/index/dispatcher.py#L55).

## Indexed retrieval

Search uses SQLite FTS5,stemming,BM25 and optional query synonyms.

| Part | Behavior |
|---|---|
| Trigger | SearchDispatcher.search |
| Mechanism | FTSIndex search |
| State | `search/fts.db,synonyms.json` |
| Result | Ranked component hits |
| Operator action | Calibration queries |

The default dispatcher uses the FTS index and configured synonyms through a NullEmbedder path.

Implementation: [FTSIndex](../src/ara/forge/index/builder.py#L51), [search](../src/ara/forge/index/dispatcher.py#L55).

## Task preflight

Run Task requires complete valid equipment and active scope.

| Part | Behavior |
|---|---|
| Trigger | Armory Run Task |
| Mechanism | check_active_trim_for_task |
| State | `armor,weapon,trim,temper state` |
| Result | Start accepted or specific preflight failure |
| Operator action | Operator corrects selected loadout |

Requires active_testing,valid/nonexpired window when specified,and active first-class or sealed/materialized classic Temper.

Implementation: [check_active_trim_for_task](../src/ara/task_preflight.py#L25), [_start_task_session](../src/ara/armory/app.py#L1084).

## Persistent task progress

Persistent task debt constrains target actions and records commits.

| Part | Behavior |
|---|---|
| Trigger | Target action or typed commit |
| Mechanism | TaskRunSession.record_action/record_commit |
| State | `task-runs/<id>/session.json,audit` |
| Result | WORK,COMMIT_REQUIRED,BRANCH_REQUIRED,VERIFY_REQUIRED,BLOCKED |
| Operator action | Courtyard task detail and recovery |

Loadout hash mismatch blocks target actions; budgets configurable.

Implementation: [record_action](../src/ara/task_session.py#L173), [record_commit](../src/ara/task_session.py#L273), [current_loadout_matches](../src/ara/task_session.py#L415).

## Durable Hermes work

Armory default Hermes worker runs a persistent controlled loop.

| Part | Behavior |
|---|---|
| Trigger | Armory Start |
| Mechanism | _build_durable_hermes_worker and ara launch |
| State | `task-launches,deadline,task/session stores` |
| Result | Hermes continue turns plus debt/transition/verify repairs |
| Operator action | Start/stop/focused task/extension controls |

The persistent task loop belongs to the default Hermes path. A custom worker uses its selected integration.

Implementation: [_build_durable_hermes_worker](../src/ara/armory/app.py#L403), [_start_task_session](../src/ara/armory/app.py#L1084), [repair_commit_debt](../src/ara/task_repair.py#L40).

## Session preparation and worker launch

Session launch prepares environment,guards,proxy and worker.

| Part | Behavior |
|---|---|
| Trigger | ara launch/claude/openclaw |
| Mechanism | launch and build_contained_env |
| State | `sessions/<id>/contain,session socket,logs` |
| Result | Supervised worker process |
| Operator action | Session status and stop |

Native injection,Node hook and Linux seccomp paths have distinct host requirements.

Implementation: [launch](../src/ara/launch.py#L94), [build_contained_env](../src/ara/containment.py#L73).

## Scoped Bulwark policy

Bulwark selects deny/approval/allow by scoped precedence.

| Part | Behavior |
|---|---|
| Trigger | Guard query |
| Mechanism | _evaluate_bulwark and evaluate |
| State | `bulwark.md,policy_hits.jsonl` |
| Result | Decision returned to guard |
| Operator action | Rule editor/list/approval |

Default when unmatched is allow; parsed rewrite actions are not returned by deterministic precedence walk.

Implementation: [_on_query](../src/ara/supervisor.py#L781), [evaluate](../src/ara/bulwark.py#L820).

## Engagement operating controls

Trim adds operating windows,stop latches,classification and rate checks.

| Part | Behavior |
|---|---|
| Trigger | Supported target operation |
| Mechanism | evaluate_call |
| State | `TOD state,stop latch,tool registry,rate buckets` |
| Result | Permit,deny,classification or token requirement |
| Operator action | TOD decisions and tool classification review |

Cannot soften Bulwark deny in composed trim gate; only supported dispatch paths participate.

Implementation: [evaluate_call](../src/ara/runtime/trim_gate.py#L109), [evaluate_gate](../src/ara/runtime/tod.py#L376), [acquire](../src/ara/runtime/rate_bucket.py#L223).

## Approval and danger handling

Approval outcomes depend on explicit operator and automation policy.

| Part | Behavior |
|---|---|
| Trigger | Approval decision |
| Mechanism | _on_query,wait_for_resolution |
| State | `approvals.jsonl,optional persistent rule` |
| Result | Allowed/denied operation |
| Operator action | Approvals,settings and audit |

Routine auto-grant configurable; unattended task path has shadow danger attestation; irreversible danger is hard denied.

Implementation: [_on_query](../src/ara/supervisor.py#L781), [_shadow_attest_danger](../src/ara/supervisor.py#L1095), [wait_for_resolution](../src/ara/approvals.py#L265).

## Model instruction composition

ABSOLUTE composes armor and Temper instructions per model request.

| Part | Behavior |
|---|---|
| Trigger | Proxy/supervisor prompt interception |
| Mechanism | active_absolute_rule,apply_rewrite_spec |
| State | `armor absolutes,active-tempers,task projection` |
| Result | Replaced/appended supported request envelope or shadow variant |
| Operator action | Rhema envelope and selected variants |

Model/protocol proxy path must be active; this is prompt mediation separate from resource enforcement.

Implementation: [active_absolute_rule](../src/ara/_rhema_shim.py#L242), [apply_rewrite_spec](../src/ara/_rhema_shim.py#L165), [_try_apply_request_rewrite](../src/ara/mitm_proxy.py#L1594).

## Canary observation and reply controls

Canary values can be detected and blocked by a matching reply rule.

| Part | Behavior |
|---|---|
| Trigger | Canary seed and model reply |
| Mechanism | canary scan and proxy reply Bulwark check |
| State | `canaries registry,generated value,rule` |
| Result | Observation or blocked reply depending policy |
| Operator action | Canary-guided rule creation,policy/audit |

Detection does not alone create universal blocking; reply inhibition and capture path influence behavior.

Implementation: [add](../src/ara/canary.py#L188), [_check_canaries_in_reply](../src/ara/mitm_proxy.py#L1244), [_try_enforce_bulwark_response](../src/ara/mitm_proxy.py#L1289).

## Shadow comparison and observation mode

Shadow comparison runs an alternate model request; armor shadow mode changes deny behavior.

| Part | Behavior |
|---|---|
| Trigger | Absolute shadow variant or armor shadow_mode |
| Mechanism | _dispatch_shadow and supervisor shadow branch |
| State | `transcript pair records,policy shadow tags` |
| Result | Comparable replies or logged would-deny operation |
| Operator action | Rhema comparisons,promote selected variant |

Shadow compare may issue an additional provider request; distinct from enforced blocking.

Implementation: [_dispatch_shadow](../src/ara/mitm_proxy.py#L2443), [_on_query](../src/ara/supervisor.py#L781).

## Procedure graph and channel surface

Chunk graph controls available channels and allowed transitions.

| Part | Behavior |
|---|---|
| Trigger | chunks.emit_tag |
| Mechanism | ChunkRuntime.emit_tag and supervisor surface check |
| State | `chunk_history.jsonl,activation.txt` |
| Result | New chunk and activation or rejection |
| Operator action | Chunk graph/current task display,operator overrides |

Graph comes from sealed weapon and is mounted per supervisor installation.

Implementation: [emit_tag](../src/ara/runtime/chunks/runtime.py#L202), [_on_channel_call](../src/ara/supervisor.py#L1549), [mint](../src/ara/runtime/activation.py#L56).

## Chunk inhibition profiles

Chunk gems provide phase-dependent inhibition profiles.

| Part | Behavior |
|---|---|
| Trigger | Active sealed chunk |
| Mechanism | resolve_inhibition_profile,_apply_inhibition_to_payload |
| State | `chunk_inhibition.json` |
| Result | Guard payload can omit explicitly relaxable non-scope rules |
| Operator action | Chunk/gem review |

Actual relaxation requires ARA_CHUNK_INHIBITION_ENFORCE; missing state defaults strict; reply denial relaxation has separate proxy path.

Implementation: [resolve_inhibition_profile](../src/ara/runtime/chunks/inhibition.py#L50), [_apply_inhibition_to_payload](../src/ara/supervisor.py#L452), [_reply_deny_relaxed_by_inhibition](../src/ara/mitm_proxy.py#L1358).

## Knowledge-gap consultation interface

Crumb intent has an available fact-coverage consultation API.

| Part | Behavior |
|---|---|
| Trigger | Explicit crumb.intent channel where installed |
| Mechanism | has_sufficient_coverage and CrumbIntentChannel |
| State | `facts,CrumbTracker,one-time token` |
| Result | fire or skip with reason |
| Operator action | Operator forced-run token |

An integration installs the consultation channel with a fact store. The default engagement installation does not automatically schedule procedures through this API.

Implementation: [CrumbIntentChannel](../src/ara/runtime/chunks/crumb_intent.py#L58), [has_sufficient_coverage](../src/ara/runtime/knowledge_gap.py#L58), [install_engagement](../src/ara/runtime/bootstrap.py#L175).

## Content-addressed evidence

Evidence Binder stores bytes by SHA256 with searchable source metadata.

| Part | Behavior |
|---|---|
| Trigger | Tool exchange/native output/model proxy capture |
| Mechanism | EvidenceBinder.put and ContentStore |
| State | `evidence/sha256,evidence/index.db` |
| Result | Stable evidence ID and activation/source metadata |
| Operator action | Evidence search/get and operator attachments |

Supported capture paths contribute evidence. Repeated identical content reuses the existing evidence record.

Implementation: [put](../src/ara/runtime/binder/api.py#L44), [capture_tool_exchanges](../src/ara/runtime/binder/tool_capture.py#L108), [_capture_evidence](../src/ara/mitm_proxy.py#L2396).

## Grounded fact records

Facts trace to evidence or existing facts.

| Part | Behavior |
|---|---|
| Trigger | fact.assert |
| Mechanism | FactAssertChannel.validate/execute |
| State | `facts.jsonl` |
| Result | Structured predicate/value with activation and dependencies |
| Operator action | Fact history/neighborhood |

Direct evidence needs response-bearing primary; enrichment is additive best-effort.

Implementation: [FactAssertChannel](../src/ara/runtime/facts/channels.py#L68), [enrich_evidence](../src/ara/runtime/provenance.py#L84), [NeighborIndex](../src/ara/runtime/facts/neighbors.py#L29).

## Lead priority and review history

Leads combine computed priority,review history and retrievability decay.

| Part | Behavior |
|---|---|
| Trigger | lead.file/investigate/touch/dismiss/confirm |
| Mechanism | compute_priority,apply_action |
| State | `leads.jsonl` |
| Result | Prioritized question lifecycle |
| Operator action | Operator priority override/disposition |

Cross-engagement yield optional input; closure helper exists without current production caller.

Implementation: [compute_priority](../src/ara/runtime/leads/priority.py#L57), [retrievability](../src/ara/runtime/leads/decay.py#L53), [check_closure](../src/ara/runtime/leads/closure_gate.py#L26).

## Expected-shape anomaly checks

Anomaly checks compare captured bytes to the active crumb shape.

| Part | Behavior |
|---|---|
| Trigger | EvidenceBinder.put hook |
| Mechanism | AnomalyAutoFirer.on_evidence,compute_divergence |
| State | `divergence_telemetry.jsonl,anomalies` |
| Result | Anomaly emit above threshold or recorded lower divergence |
| Operator action | Promote/dismiss anomaly |

Requires active crumb,loaded expected shape,and installed hook; absence skips.

Implementation: [on_evidence](../src/ara/runtime/anomaly/autofire.py#L79), [compute_divergence](../src/ara/runtime/anomaly/divergence.py#L30).

## Finding draft validation

Finding file checks referenced evidence and explicit/derived claim markers.

| Part | Behavior |
|---|---|
| Trigger | finding.file |
| Mechanism | validate_for_draft,_rule_claim_verification |
| State | `findings.jsonl,field-store objects` |
| Result | Draft or detailed rejection |
| Operator action | Spoils review and enrichment |

Optional forensic fields tolerated; explicit markers stricter than derived heuristics; not_mechanically_testable exemption exists.

Implementation: [FindingFileChannel](../src/ara/runtime/findings/channels.py#L333), [_rule_claim_verification](../src/ara/runtime/findings/validator.py#L367), [record_finding_fields](../src/ara/runtime/findings/field_store.py#L75).

## Operator finding sign-off

Operator signoff applies stronger content and evidence requirements.

| Part | Behavior |
|---|---|
| Trigger | finding.sign_off |
| Mechanism | validate_for_signoff and allowed_callers operator |
| State | `finding signed_off record/content hash` |
| Result | Reviewed finding eligible for report |
| Operator action | Operator signoff/rescind |

Reason,impact,fix,assets,confirmation,evidence required; pack-driven content expectations are mechanical checks,not semantic truth proof.

Implementation: [FindingSignOffChannel](../src/ara/runtime/findings/channels.py#L486), [validate_for_signoff](../src/ara/runtime/findings/validator.py#L662).

## Structured report generation

Report Kiln renders signed-off findings with evidence and coverage sections.

| Part | Behavior |
|---|---|
| Trigger | Operator report.render |
| Mechanism | ReportRenderChannel and render_report |
| State | `report.md,reports.jsonl` |
| Result | Structured Markdown and section diagnostics |
| Operator action | Operator report review |

Consumes signed-off findings,not every draft; styles are optional registration.

Implementation: [ReportRenderChannel](../src/ara/runtime/report/channels.py#L25), [render_report](../src/ara/runtime/report/kiln.py#L119), [StyleScoreChannel](../src/ara/runtime/style/channels.py#L21).

## Reference-run baselines

Baseline captures observed resources and can generate an armor.

| Part | Behavior |
|---|---|
| Trigger | baseline capture/compare/to-armor |
| Mechanism | baseline extraction/diff/materialization |
| State | `baselines records,generated armor` |
| Result | Reviewable differences and policy candidate |
| Operator action | Baseline comparison and resulting armor review |

Recorded resource coverage is bounded by captured session events.

Implementation: [record_from_session](../src/ara/baseline.py#L135), [diff_against_session](../src/ara/baseline.py#L162), [to_armor](../src/ara/baseline.py#L222).

## Session archive and attestation

Session export includes audit attestation and detached shared-secret HMAC.

| Part | Behavior |
|---|---|
| Trigger | Session export/export.sig |
| Mechanism | verify_chain,build_session_export_signature |
| State | `tarball,ATTESTATION.json,signature` |
| Result | Portable selected session records and authenticity check |
| Operator action | Operator chooses export/verification |

The archive uses a shared-secret HMAC. It contains selected session records and the contained directory tree.

Implementation: [build_session_export](../src/ara/armory/app.py#L1414), [build_session_export_signature](../src/ara/armory/app.py#L1506).

## Focused work and reviewed evolution

Review can lead to focused investigation,baseline policy and versioned reforge.

| Part | Behavior |
|---|---|
| Trigger | Spoils polish/review/Ref orge |
| Mechanism | _start_polish_task,reforge proposal approval |
| State | `reused session stores,typed proposal` |
| Result | Updated reviewed record or new candidate equipment |
| Operator action | Operator accept/reject,seal/equip |

The operator reviews and promotes artifact revisions for later use.

Implementation: [_start_polish_task](../src/ara/armory/app.py#L1025), [build_directive](../src/ara/armory/polish.py#L154), [propose](../src/ara/forge/reforge/driver.py#L55).

## Contained Hermes gateway reconciliation

The gateway guard detects uncontained Hermes gateways and reconciles the configured contained service.

| Part | Behavior |
|---|---|
| Trigger | Armory guard thread on the supported host |
| Mechanism | Process ancestry checks,service-unit reconciliation,and circuit-breaker state |
| State | hermes_guard.json,configured service unit,guard alert state |
| Result | Recorded drift,contained restart,or held-off state |
| Operator action | Guard alert and service configuration |

The production service path uses Linux process and user-service facilities. Its paths and service configuration belong to the deployment.

Implementation: [find_uncontained_hermes](../src/ara/hermes_guard.py#L86), [run_reconcile](../src/ara/hermes_guard.py#L329), [start_hermes_guard_thread](../src/ara/armory/app.py#L1384).

## Skill change observation

Skill snapshots identify added,removed,and modified configured skill directories.

| Part | Behavior |
|---|---|
| Trigger | Armory startup scan or explicit scan call |
| Mechanism | Directory fingerprints and snapshot comparison |
| State | skill_index.json,skill_changed events |
| Result | Visible change events with hash and size metadata |
| Operator action | Activity and policy review |

The scan examines the configured skill roots. It records changes for review.

Implementation: [scan](../src/ara/skill_watch.py#L246), [changes_to_events](../src/ara/skill_watch.py#L259).

## Optional MCP traffic scanner

An installed MCP scanner can send findings into the ARA event stream.

| Part | Behavior |
|---|---|
| Trigger | Static configuration scan or configured scanner proxy port |
| Mechanism | External scanner process and finding parser |
| State | scanner log and mcp_scan_finding events |
| Result | Scanner observations available for review |
| Operator action | Configure the scanner and inspect its findings |

The scanner executable must be installed. The operator routes the MCP client through the selected proxy for traffic coverage.

Implementation: [run_static_scan](../src/ara/mcp_scan.py#L93), [launch_proxy_sidecar](../src/ara/mcp_scan.py#L117).
