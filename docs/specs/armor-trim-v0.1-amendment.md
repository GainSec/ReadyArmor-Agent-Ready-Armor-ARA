# Engagement controls and knowledge consultation

The engagement context connects permission, operating conditions, selected materials, and task progress.
Trim metadata supplies concrete scope and operating values.
The supervisor uses the applicable runtime gates around supported worker operations.

## Operating contract

The Trim gate checks its relevant conditions in sequence:

1. Examine the time-of-day window.
2. Examine the configured stop latch.
3. Examine tool classification and review state.
4. Apply the applicable activity and authorization conditions.
5. Apply the configured rate bucket.

The composed gate can hold or reject an otherwise permitted operation.
It does not soften a Bulwark denial.
The task controller separately checks progress obligations and equipment continuity.
The supervisor retains its prior policy decision when Trim evaluation fails.
A rate overflow of `deny` can reject the operation.
The API also returns queue delays; the current supervisor does not sleep for those delays.
See [Control in Depth](../control-in-depth.md) for consumer-specific behavior.

## Materials and existing facts

KEEP preserves engagement materials in explicit classes.
Selected material classes inform engagement preparation.
Facts can record observations derived from evidence or prior facts.

A procedure can declare which fact shapes it would produce.
The knowledge-gap function checks the configured minimum counts for every declared requirement.
Complete coverage returns a skip decision.
Partial coverage returns a fire decision with missing requirements.

The `crumb.intent` integration can apply that decision before procedure execution.
An integration supplies the fact store and installs the channel.
A one-time operator token can request the procedure despite existing knowledge.
The default engagement installation does not automatically schedule procedures through this API.

## Separate context and hardening

Concrete targets, time windows, authorizations, and cadence belong to the Trim.
Reusable hardening belongs to a reviewed Temper package.
The current Armory creation page installs curated hardening.
Tailor and Needle candidate machinery remains a separate programmatic interface.

## Sources and detailed guides

- [Trim operating gate](../../src/ara/runtime/trim_gate.py#L109).
- [Knowledge-gap decision](../../src/ara/runtime/knowledge_gap.py#L58).
- [Crumb intent channel](../../src/ara/runtime/chunks/crumb_intent.py#L58).
- [Engagement installation](../../src/ara/runtime/bootstrap.py#L175).
- [Cognitive mechanisms](../cognitive-mechanisms.md).
- [Equipment](../equipment.md).
