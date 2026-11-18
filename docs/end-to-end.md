# End-to-end workflows

ARA connects source preparation, equipment creation, task execution, and reviewed output.
The workflows below show the records produced at each boundary.
Use one state directory throughout a workflow.

## Workflow 1: source material to sealed weapon

The input is an approved procedure or source collection.
The output is a reviewed equipment artifact with identifiable source relationships.

1. Open Forge from Armory.
2. Select weapon creation and the domain Pack.
3. Enter the new weapon identifier.
4. Deposit the source materials.
5. Generate the manifest.
6. Read the manifest elements and exclude irrelevant content.
7. Approve the manifest for extraction.
8. Run the selected extraction path.
9. Examine coverage and the structured draft.
10. Resolve conflicting or incomplete entries.
11. Request seal after review.
12. Examine the written weapon, seal metadata, and search results.

Each deposited file has a recorded identity.
Manifest elements preserve source text and format metadata.
Extraction events connect their structured bodies to those elements.
Synthesis places the events into component groups and projected alphabets.
Seal writes policy, chunks, vocabularies, indexes, and metadata.

The separate calibration command measures retrieval against expected results.
Include the representative queries that the method will need during use.

### Command sequence

Create the session:

```sh
ara forge --state-root .ara new web-method
```

Record the returned `forge_id` as `FORGE_ID` in the shell.
Deposit the approved material:

```sh
ara forge --state-root .ara deposit "$FORGE_ID" --mat materials/method.md
ara forge --state-root .ara finalize-deposit "$FORGE_ID"
ara forge --state-root .ara manifest "$FORGE_ID"
```

Review the manifest in Armory before approval.
Then use the session identifier for each phase operation:

```sh
ara forge --state-root .ara approve-manifest "$FORGE_ID"
ara forge --state-root .ara run-exhaust "$FORGE_ID"
ara forge --state-root .ara coverage "$FORGE_ID"
ara forge --state-root .ara synthesize "$FORGE_ID"
```

Review the draft before seal:

```sh
ara forge --state-root .ara seal "$FORGE_ID"
ara weapon --state-root .ara list
ara weapon --state-root .ara calibrate web-method --queries calibration_queries.json
```

The source file, worker runtime, and calibration file must exist at their specified locations.
Use the active MCP tool list for the chosen extraction mode.

Implementation: [deposit_mat](../src/ara/forge/phases/deposit.py#L45), [run_manifest](../src/ara/forge/phases/manifest.py#L27), [make_element_id](../src/ara/forge/manifest/element.py#L61), [_merge_with_prior_drops](../src/ara/forge/phases/manifest.py#L70), [record_extraction](../src/ara/forge/phases/exhaust.py#L65), [run_exhaust](../src/ara/forge/loop_driver.py#L292), [run_exhaust_stateless](../src/ara/forge/loop_driver.py#L1017), [synthesize](../src/ara/forge/phases/synthesize.py#L223), [project](../src/ara/forge/alphabets/projector.py#L255), [seal](../src/ara/forge/phases/seal.py#L266), [validate_manifest_coverage](../src/ara/forge/phases/seal.py#L107), [validate_citation_integrity](../src/ara/forge/phases/seal.py#L119), [_cmd_calibrate](../src/ara/weapon_cli.py#L121), [run_calibration](../src/ara/forge/index/calibration.py#L70), [search](../src/ara/forge/index/dispatcher.py#L55).

## Workflow 2: materials to a reusable domain Pack

A Pack defines reusable vocabulary and workflow structure.
It becomes an input to later weapon creation.

1. Select Pack creation in Forge.
2. Enter the Pack identifier and baseline workflow.
3. Deposit material that defines the domain's terms and method.
4. Generate and review the manifest.
5. Start Pack extraction through the Codex and Pen tool surface.
6. Examine the proposed alphabet shapes and entries.
7. Resolve duplicate or contradictory definitions.
8. Examine the proposed workflow chunks and policy defaults.
9. Keep, edit, remove, or add draft items through curation.
10. Seal the reviewed Pack.
11. Inspect the runtime Pack entry.
12. Select that Pack for later weapon creation.

Pack mode exposes vocabulary proposal tools.
Reconciliation mode exposes specific merge, split, cell-resolution, and drop tools.
Workflow derivation uses the procedure-node interface.
This separates three authoring jobs with different outputs.

The sealed Pack is stored in the runtime Pack directory.
Its definition can supply categorical, matrix, template-set, and vector alphabets.
Its workflow templates and policy defaults are materialized into later equipment where selected.

Implementation: [AlphabetPack](../src/ara/forge/alphabets/pack.py#L131), [build_pack_draft](../src/ara/forge/pack_draft.py#L182), [seal_pack](../src/ara/forge/phases/seal.py#L230), [detect_conflict_groups](../src/ara/forge/reconcile.py#L129), [apply_decision](../src/ara/forge/reconcile.py#L218), [synthesize](../src/ara/forge/phases/synthesize.py#L223), [project](../src/ara/forge/alphabets/projector.py#L255).

## Workflow 3: engagement preparation to a supervised task

The input is a target engagement and a suitable sealed method.
The output is a persistent task run with explicit equipment and operating context.

1. Open KEEP for the engagement.
2. Add the approved documents, source, artifacts, and credentials to their classes.
3. Select the materials that belong to the run.
4. Select or create the persistent armor identity.
5. Equip the reviewed weapon.
6. Open Sew and enter the engagement context.
7. Generate and examine the Trim.
8. Apply the Trim.
9. Install and equip the reviewed Temper hardening package.
10. Examine the complete loadout and operating window.
11. Enter the task objective in Armory.
12. Start the task after preflight passes.

Current task preflight checks the active armor, sealed weapon, active Trim, active-testing permission, and applicable end time.
It also checks the equipped or supported materialized Temper.
A missing prerequisite produces a specific startup result.

The task record stores the equipment identities and checked hashes.
The handler prepares the instruction projection and contained worker command.
The default Hermes path uses the persistent task loop.
A custom worker command uses its selected integration path.

### What the operator sees

Courtyard shows task and session activity.
The task detail shows the current chunk and available progress information.
Approval and tool-classification records appear through their corresponding controls.
Rhema shows the captured model interaction and selected instruction envelope.

Implementation: [ensure_engagement](../src/ara/engagement_mats.py#L125), [add_mat_bytes](../src/ara/engagement_mats.py#L235), [set_mat_selected](../src/ara/engagement_mats.py#L347), [parse_derive_form](../src/ara/armory/trim_views.py#L573), [derive_trim](../src/ara/trims.py#L806), [apply_trim](../src/ara/trims.py#L643), [handle_temper_create](../src/ara/armory/temper_views.py#L498), [handle_temper_tailor_start](../src/ara/armory/temper_views.py#L549), [ingest](../src/ara/tempers.py#L118), [equip](../src/ara/tempers.py#L247), [check_active_trim_for_task](../src/ara/task_preflight.py#L25), [_start_task_session](../src/ara/armory/app.py#L1084), [record_action](../src/ara/task_session.py#L173), [record_commit](../src/ara/task_session.py#L273), [current_loadout_matches](../src/ara/task_session.py#L415), [_build_durable_hermes_worker](../src/ara/armory/app.py#L403), [repair_commit_debt](../src/ara/task_repair.py#L40).

## Workflow 4: operation to reviewed report

The input is an operation performed by the supervised worker.
The output is an evidence-backed record and, after review, a report entry.

1. The worker requests an operation through its supported interface.
2. The interception path sends the applicable query to the supervisor.
3. Policy, engagement, task, and danger checks produce a decision.
4. The runtime records the decision and observed result through its available capture path.
5. The Binder stores the captured bytes and metadata.
6. The worker submits a fact, lead, or finding through a typed channel.
7. The channel checks its input and applicable evidence conditions.
8. The operator examines the result in Spoils.
9. The operator resolves incomplete fields or requests focused investigation.
10. The operator signs off a supported finding.
11. The Report Kiln renders the signed-off findings.
12. The operator examines the report sections and diagnostics.

A recorded connection or command is not sufficient proof of the target's response.
Directly cited facts and findings use the applicable response-bearing evidence checks.
Claim verification can also inspect required and excluded markers in the stored bytes.

A draft may remain incomplete during investigation.
Sign-off adds the necessary impact, remediation, asset, confirmation, and evidence content.
The report consumes signed-off findings in severity order.
It also includes the template's methodology, evidence, and coverage sections.

Implementation: [_on_query](../src/ara/supervisor.py#L781), [evaluate](../src/ara/bulwark.py#L820), [put](../src/ara/runtime/binder/api.py#L44), [capture_tool_exchanges](../src/ara/runtime/binder/tool_capture.py#L108), [_capture_evidence](../src/ara/mitm_proxy.py#L2396), [FactAssertChannel](../src/ara/runtime/facts/channels.py#L68), [enrich_evidence](../src/ara/runtime/provenance.py#L84), [NeighborIndex](../src/ara/runtime/facts/neighbors.py#L29), [FindingFileChannel](../src/ara/runtime/findings/channels.py#L333), [_rule_claim_verification](../src/ara/runtime/findings/validator.py#L367), [record_finding_fields](../src/ara/runtime/findings/field_store.py#L75), [FindingSignOffChannel](../src/ara/runtime/findings/channels.py#L486), [validate_for_signoff](../src/ara/runtime/findings/validator.py#L662), [ReportRenderChannel](../src/ara/runtime/report/channels.py#L25), [render_report](../src/ara/runtime/report/kiln.py#L119), [StyleScoreChannel](../src/ara/runtime/style/channels.py#L21).

## Workflow 5: reviewed work to improved equipment

The input is an observed weakness, useful result, or revised source material.
The output is a reviewed change for later use.

1. Examine the existing finding, lead, fact, or method.
2. Select the improvement that needs further work.
3. Use focused work for a specific unresolved record when appropriate.
4. Examine the new evidence and updated record.
5. Capture a reference baseline when the observed resource set is useful.
6. Compare later activity with that baseline.
7. Review any proposed standing hardening separately from task-specific Trim values.
8. Create a new Forge session with the predecessor weapon when the method needs revision.
9. Generate the revised draft from the selected sources.
10. Examine the typed Reforge differences and their rationales.
11. Approve the proposal and seal the reviewed revision.
12. Select the new equipment for a later task.

Focused work can reuse the earlier session records.
It has an explicit objective and time budget.
A deadline extension is a recorded request or grant.

Reforge records component additions, removals, and modifications.
It can also carry calibration-query changes and rationales.
Approval returns the session to review for the later seal operation.
This makes capability growth an artifact lifecycle under operator control.

Implementation: [_start_polish_task](../src/ara/armory/app.py#L1025), [build_directive](../src/ara/armory/polish.py#L154), [propose](../src/ara/forge/reforge/driver.py#L55), [record_from_session](../src/ara/baseline.py#L135), [diff_against_session](../src/ara/baseline.py#L162), [to_armor](../src/ara/baseline.py#L222), [call_tool](../src/ara/clients/temper_mcp_server.py#L514), [_validate_tailor_candidate](../src/ara/temper_lifecycle.py#L195), [add_tailor_coverage_summary](../src/ara/temper_lifecycle.py#L594), [validate_control](../src/ara/temper_controls.py#L153).

## A complete worked engagement

Assume an operator has an approved web method, an application target, and a defined operating window.
The method becomes `web-method` through the first workflow.
The operator examines its chunks, vocabulary, policy, and retrieval results.

The operator then creates the engagement workspace in KEEP.
The selected documents and application material stay associated with that engagement.
Sew turns the target, excluded assets, time window, and activity posture into a Trim.
The operator applies that Trim with the selected armor, weapon, and hardening package.

Preflight accepts the complete loadout.
The persistent task record binds the checked equipment to the run.
Hermes starts inside the prepared session and receives the active procedure context.
The chunk graph and task debt rules control the available structured progress operations.

Suppose a probe produces a response that suggests an access-control weakness.
The captured reply becomes a Binder object.
A fact records the observation.
A lead records the unresolved authorization question.
Further investigation can then supply a finding with the supporting response and explicit verification conditions.

The operator examines the raw capture and the proposed impact.
If the finding is incomplete, focused work can add evidence or challenge the result.
Sign-off applies the stronger content requirements.
The Report Kiln includes the signed-off finding and its evidence context.

A useful method correction can then enter a Reforge proposal.
The next engagement receives a reviewed method revision and its own separate Trim.
The procedure, permission, observation, and accepted result each retain their own record.
