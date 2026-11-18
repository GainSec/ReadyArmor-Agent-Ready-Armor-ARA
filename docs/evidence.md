# Evidence, findings, and reviewed output

ARA preserves observation, interpretation, review, and accepted output as separate records.
The Evidence Binder stores captured material.
Typed channels create facts, leads, findings, and anomalies around that material.
Operator review determines which supported findings enter a report.

## Captured evidence

The Binder stores bytes under their SHA-256 content hash.
Its SQLite index stores evidence identity, source, source metadata, capture time, activation, size, type, and excerpt.
Repeated identical bytes reuse the content identity.

Supported capture paths include model proxy observations, tool exchanges, and native process output.
A captured command and its observed output have different evidential roles.
The response-bearing check requires actual observed result material for the applicable direct-evidence assertion.

The operator can search or read stored evidence through its channels and interface.
Spoils also accepts operator attachments that become Binder records linked to a finding.

The CLI provides an additional file evidence index and hash verification interface:

```sh
ara evidence record --help
ara evidence list --help
ara evidence verify --help
```

Use the matching evidence interface for the record being reviewed.
Keep file locations intact when a file-index verification depends on them.

## Activation and provenance

An activation identifies the current execution context.
Chunk initialization and transitions create activation records.
Captures can record their activation identity.
Freshness checks can examine evidence captured or accessed within the current activation.

The provenance enrichment path can add the latest applicable response-bearing observation.
It can also attach connection material and the active authorization projection.
These additions preserve useful relationships without replacing the underlying captured bytes.

Evidence identity answers which content was stored.
Source metadata answers how the content entered the store.
Activation answers the execution context associated with its capture or use.
The finding's references connect that material to a claim.

## Facts

A fact has a predicate, value, origin, confidence, procedure reference, and supporting relationships.
`fact.assert` requires evidence references or dependencies on existing facts.
The channel checks referenced evidence and dependencies.
Direct evidence assertions use the applicable response-bearing evidence condition.

Fact history preserves updates.
Neighborhood queries expose dependency, temporal, causal, and evidential relationships.
These functions support later investigation without flattening all observations into one summary.

## Leads

A lead holds a question for further investigation.
It can reference facts, evidence, and its originating procedure or anomaly.
Its lifecycle includes investigation, review, dismissal, confirmation, and operator priority changes.

Computed priority uses explicit signals.
These include evidence strength and diversity, value tier, procedure provenance, scope weight, and optional prior yield.
Retrievability decreases with elapsed time according to the lead's stability.
Review actions update its state and history.

This keeps unresolved work visible as unresolved work.
A lead's presence is not a confirmed finding.

## Anomalies

An expected evidence shape describes the form associated with a procedure.
An installed capture hook can compare observed content with the active crumb's shape.
Divergence can include missing fields, pattern failures, unexpected fields, structural differences, and line or section mismatches.

A score below the configured threshold remains telemetry.
A score at or above the threshold can create an anomaly record.
The operator can promote that record to investigation or dismiss it.
The comparison needs an active crumb and loaded shape.

## Finding draft acceptance

`finding.file` normalizes the submitted body and checks its applicable schema and semantic conditions.
The checks include evidence resolution, response-bearing primary evidence, linked records, freshness, and claim content where applicable.
Some forensic fields remain optional while the record is a draft.

For an explicit verification object, all specified required markers must occur in the cited bytes.
Excluded markers must not occur.
Derived marker checks use available claim text with their defined heuristic behavior.
A finding marked `not_mechanically_testable` uses its separate review treatment.

These checks make the submitted claim inspectable against actual content conditions.
They operate alongside the operator's evaluation of scope, significance, and interpretation.

## Field-level records and enrichment

Finding filing can record individual field values as content-addressed objects.
The fields include impact, remediation, confirmation, reason, and affected assets.
These objects provide an additional diff and integrity surface around the inline finding values.

Enrichment channels can add proposed information without directly replacing operator acceptance.
The operator can review enrichment through the corresponding review channel.
This preserves the distinction between a proposed improvement and the accepted finding state.

## Operator sign-off

Sign-off is an operator-only channel operation.
It accepts a finding in a supported review state and applies the stronger sign-off rules.

The required record includes:

- A reason or description.
- Structured impact.
- A remediation or fix.
- Affected assets.
- The confirmation method.
- Raw evidence references.

Field content expectations can add mechanical requirements for length, meaningful keywords, lists, or configured scope membership.
Applicable Pack definitions can supply these expectations.
A successful sign-off writes its actor, time, content hash, and lifecycle state.
Referenced leads can then be confirmed through the system channel.

Rescind remains a distinct later action.
The audit and record history preserve the lifecycle changes.

## Spoils and focused work

Spoils groups reviewable findings, leads, facts, evidence links, and their associated loadout.
The operator can change a review disposition or attach further evidence.
Focused work starts a bounded task against one selected record.

For example, a confirmation task can reproduce an existing finding and update its evidence.
A deeper verification task can actively challenge the claim.
The task objective, deadline, resulting captures, and record changes remain visible through the existing stores.

## Report generation

`report.render` is an operator channel.
The Report Kiln selects signed-off findings and sorts them by severity.
It combines them with the relevant evidence, leads, and coverage context.

A weapon template or the default template defines the report sections.
The renderer writes Markdown, section diagnostics, a report record, and a content hash.
Optional style scoring can examine text against a loaded weapon style profile when that channel is installed.

Review the section diagnostics as well as the report text.
The sign-off record and the report record have separate purposes and identifiers.

## Audit and export

Policy and runtime audit functions preserve verifiable record sequences.
Use the verification command for the relevant session audit chain:

```sh
ara session list --state-root .ara
ara verify-audit all --state-root .ara
```

The session archive contains selected session records and the contained directory tree.
It also includes audit-chain attestation at export time.
The detached archive signature is an HMAC using the shared administrator secret.
Verification uses that same secret.

Binder files and rendered reports have their own locations in session state.
Retain the relevant evidence and report files when assembling a complete review package.

Implementation: [put](../src/ara/runtime/binder/api.py#L44), [capture_tool_exchanges](../src/ara/runtime/binder/tool_capture.py#L108), [_capture_evidence](../src/ara/mitm_proxy.py#L2396), [FactAssertChannel](../src/ara/runtime/facts/channels.py#L68), [enrich_evidence](../src/ara/runtime/provenance.py#L84), [NeighborIndex](../src/ara/runtime/facts/neighbors.py#L29), [compute_priority](../src/ara/runtime/leads/priority.py#L57), [retrievability](../src/ara/runtime/leads/decay.py#L53), [check_closure](../src/ara/runtime/leads/closure_gate.py#L26), [on_evidence](../src/ara/runtime/anomaly/autofire.py#L79), [compute_divergence](../src/ara/runtime/anomaly/divergence.py#L30), [FindingFileChannel](../src/ara/runtime/findings/channels.py#L333), [_rule_claim_verification](../src/ara/runtime/findings/validator.py#L367), [record_finding_fields](../src/ara/runtime/findings/field_store.py#L75), [FindingSignOffChannel](../src/ara/runtime/findings/channels.py#L486), [validate_for_signoff](../src/ara/runtime/findings/validator.py#L662), [ReportRenderChannel](../src/ara/runtime/report/channels.py#L25), [render_report](../src/ara/runtime/report/kiln.py#L119), [StyleScoreChannel](../src/ara/runtime/style/channels.py#L21), [build_session_export](../src/ara/armory/app.py#L1414), [build_session_export_signature](../src/ara/armory/app.py#L1506), [_start_polish_task](../src/ara/armory/app.py#L1025), [build_directive](../src/ara/armory/polish.py#L154), [propose](../src/ara/forge/reforge/driver.py#L55).
