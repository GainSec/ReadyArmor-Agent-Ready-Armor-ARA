# Operator guide

## Start a supervised session

1. Start Armory with the intended state directory.
2. Open the Armors page.
3. Select the armor for the task.
4. Examine its Bulwark rules.
5. Select its ABSOLUTE instructions.
6. Start the worker with the same state directory.

```sh
ara launch --state-root .ara --contain --contain-profile hermes-gateway -- hermes gateway run
```

The worker executable must already be installed.
The `--contain` option prepares separate session directories for worker configuration and temporary files.

## Select equipment

1. Open Armory.
2. Select an armor.
3. Select a sealed weapon for the task method.
4. Apply a Trim for engagement context.
5. Equip the reviewed Temper hardening package.
6. Examine the effective policy before the run.

A task loadout uses Armor, Weapon, Trim, and Temper together.
Current Run Task preflight checks all four roles.
See [Equipment](equipment.md) for the file structure and lifecycle.

## Respond to an approval request

1. Open the Approvals page.
2. Examine the requested operation and its target.
3. Select the decision scope.
4. Enter the reason.
5. Submit the decision.

The command interface provides the same approval actions:

```sh
ara approval list --state-root .ara
ara approval show APPROVAL_ID --state-root .ara
ara approval respond APPROVAL_ID deny --reason "Outside task scope" --state-root .ara
```

Replace `APPROVAL_ID` with the identifier from the list.
`allow-session` and `deny-session` apply to the session.
`allow-always` and `deny-always` create persistent entries.
The `--ttl` option adds an expiry to persistent entries.

## Examine a run

1. Open Courtyard to examine current sessions.
2. Open the task detail to examine its current chunk.
3. Examine the evidence and channel events.
4. Examine unresolved findings and leads.
5. Use the task controls to pause or stop the run.

Use the following commands for session and audit status:

```sh
ara status --state-root .ara
ara session list --state-root .ara
ara verify-audit all --state-root .ara
```

## Complete lifecycle

See the [end-to-end workflows](end-to-end.md) for source preparation, equipment creation, task execution, and reviewed output.
See the [system codex](codex.md) for the relationship between each operator page and its state.
