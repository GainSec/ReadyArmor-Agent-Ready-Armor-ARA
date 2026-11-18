# Task lifecycle

A task run connects an objective, equipment, a worker session, and persistent progress.
Hermes provides the persistent task execution path.
Armory presents the task state and operator controls.

## Preparation

1. Select the task objective.
2. Select an armor and sealed weapon.
3. Apply the engagement Trim.
4. Equip the reviewed Temper hardening package.
5. Examine the task preflight results.
6. Start the run from Armory.

Preflight checks the active armor, sealed weapon, active Trim, and applicable Temper state.
It also checks active-testing permission and the end time when specified.
The task controller creates the run records and worker session.

## Chunks and branches

A chunk is a bounded procedure within the equipped weapon.
It defines the available channel surface and progress conditions.
A branch records the next procedure path.
The runtime checks a requested transition against the current chunk state.

An unresolved prerequisite can stop a transition.
The task can also stop for an operator decision, an evidence requirement, or a time limit.
The task detail shows the active chunk and recorded events.

## Repair

The repair path records supported state changes through explicit channel operations.
It can complete a pending commit or repair a chunk transition.
The controller checks the current run state before it accepts the change.

1. Examine the task detail and recent channel errors.
2. Examine the evidence referenced by the current chunk.
3. Resolve the missing prerequisite through its channel.
4. Use the task recovery control when the prerequisite is complete.
5. Examine the next task event.

Use `ara debug-log --help` to select task or session filters.
Use `ara task-extend --help` for a task time extension.

## Completion

A completed run retains its task records, evidence references, findings, and audit history.
A stopped run retains its recorded progress.
Operator controls determine whether a run resumes or ends.

See [Evidence](evidence.md) and [Integrations](integrations.md).
