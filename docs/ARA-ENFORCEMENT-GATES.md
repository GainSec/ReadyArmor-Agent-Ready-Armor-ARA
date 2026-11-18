# Enforcement gates

ARA applies controls at several boundaries around the worker.
The available path depends on the runtime, host, selected policy, and integration.
The [Control in Depth guide](control-in-depth.md) traces each consumer to its actual decision and activation conditions.
Armory starts Hermes by default; OpenClaw and Claude Code are explicit integrations.

## Operation decisions

The supervisor evaluates supported operation queries through these stages:

1. Select the applicable Bulwark decision.
2. Apply the engagement operating gate.
3. Apply task progress requirements to permitted target activity.
4. Examine recognized danger conditions.
5. Resolve approval through the configured decision path.
6. Apply the relevant observation mode.
7. Record and return the final decision.

The Trim gate can make an operation more restrictive.
Task state can hold work for a commit, branch update, or verification pass.
Recognized irreversible danger has its separate deny path.

Approval handling includes operator decisions and configured automation.
Routine automatic grants and unattended task attestation have their own conditions.
Armor shadow mode can record selected would-deny outcomes while permitting the operation.

## Runtime boundaries

| Boundary | Function |
|---|---|
| Node hooks | Intercept supported JavaScript operations |
| Native shim | Intercept supported libc operations and propagate guard configuration |
| Linux seccomp notification | Supply an additional network decision path on supported launches |
| Model proxy | Inspect supported request and reply formats |
| Typed channels | Check caller, body, active chunk surface, and applicable record requirements |
| Task controller | Check progress obligations and equipment continuity |
| Operator review | Apply sign-off requirements before report use |

## Model and record decisions

ABSOLUTE composes selected instruction material for supported request envelopes.
Rhema presents the interaction and associated controls.
A shadow request supplies an alternate comparison without replacing the live reply.

Finding checks examine references and applicable content conditions against captured evidence.
Operator sign-off adds impact, remediation, asset, confirmation, and evidence requirements.
These record checks operate separately from permission for the original tool operation.

## Source and detailed guides

- [Supervisor operation path](../src/ara/supervisor.py#L781).
- [Trim gate](../src/ara/runtime/trim_gate.py#L109).
- [Task action state](../src/ara/task_session.py#L173).
- [Architecture](architecture.md).
- [Bulwark grammar](BULWARK-SPEC.md).
- [Feature reference](features.md).
