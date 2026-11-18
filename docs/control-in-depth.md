# Control in Depth

Useful agent work reaches real resources.
A command can read a credential, change a file, contact a host, or alter a target.
A model instruction describes the permitted task.
An operation gate decides whether a supported operation can proceed.
A record gate decides whether an observation can become an accepted result.

ARA connects these controls around a reusable method and an explicit engagement.
The controls examine different information at different boundaries.
A denied file call need not reach the model.
A rejected finding need not become report content.
The operator can inspect both decisions through their records.

This chapter describes architecture order, from resource operations to accepted output.
Actual call order depends on the worker, API, platform, and configured controls.

## The primary worker path

**Armory starts Hermes by default.**
Its task handler selects `hermes` when the request supplies no worker command.
The normal Hermes path uses the persistent task loop and the contained task profile.
The generic `ara launch` command starts the executable supplied after `--`.
Claude Code and OpenClaw use explicit adapters or selected custom commands.
Compatibility environment variables do not select OpenClaw as the worker.

Armory prepares a task record, equipment hashes, instruction projection, and action budgets before launch.
The launcher starts the supervisor and normally starts the session model proxy.
It prepares the guard environment and supported runtime interception paths before it starts the worker.

Sources: [Armory task selection](../src/ara/armory/app.py#L1150), [contained launch](../src/ara/armory/app.py#L1201), [generic launcher](../src/ara/launch.py#L94).

## Read the control types

| Type | Decision boundary | Practical effect |
|---|---|---|
| Operation gate | Supported resource call | Stop that call before the underlying operation |
| Policy gate | Submitted query or inspected message | Return a decision that the consumer applies |
| Task gate | Target action or channel transition | Hold the next applicable action until its task conditions pass |
| Model guidance | Supported instruction envelope | Give the model the selected identity, method, scope, and obligations |
| Observation | Captured activity or alternate output | Supply review information without independently stopping the original operation |
| Record gate | Structured record acceptance | Reject invalid input or an invalid lifecycle transition |
| Operator authority | Configuration and review interface | Select permission, equipment, dispositions, and accepted output |

An instruction, an audit event, and an operation denial have different effects.
The control type makes that effect explicit.

## 1. Installed Linux network filter

The lowest network path uses a real seccomp filter when installation succeeds.
The filter applies to the launched child and its descendants.
It selects specific syscall forms:

- `connect` enters the supervisor notification path.
- `sendto` enters that path when its destination pointer is present.
- `io_uring_setup` returns `EPERM`.
- Other syscall numbers pass this filter.

The notification handler reads the destination from the process memory.
It extracts the address family, numeric IP address, and port.
The supervisor checks projected network allow rules before projected deny rules.
An unmatched IP destination receives a deny decision by default.
An unreadable destination also receives a deny decision by default.
`ARA_SECCOMP_FAILOPEN` changes those two defaults when enabled.
Unix sockets pass this decision path.
Other decoded address families pass when no IP destination exists.

A deny response returns `EACCES` for the intercepted syscall.
The handler omits the flag that would continue the original syscall.
This stops that operation through the kernel notification interface.

### Activation

The launcher needs all of these conditions:

1. The launch uses `--contain`.
2. `ARA_SECCOMP_NET_ENFORCE` has an accepted true value.
3. Platform screening accepts the x86-64 host and kernel version.
4. The child installs the filter successfully.
5. The supervisor receives and attaches the notification descriptor.

Armory sets the enforcement flag to `1` by default for task runs.
An operator environment value can override that default.
The platform screen checks architecture and kernel version.
Actual filter installation remains a separate step.
The child catches installation errors and continues with the other configured interception paths.
The launcher also records missing descriptors and attachment failures.

The filter does not select every possible network syscall.
For example, it does not add a `sendmsg` notification rule.
Its control boundary is the selected syscall set and inherited filter installation.

Sources: [launch activation and installation](../src/ara/launch.py#L447), [filter instructions](../src/ara/seccomp_notif.py#L80), [installation](../src/ara/seccomp_notif.py#L130), [destination decision](../src/ara/supervisor.py#L1154), [deny response](../src/ara/seccomp_notif.py#L234).

## 2. Native resource interposition

The native shim replaces supported libc entry points inside a process that loads the library.
Linux uses `LD_PRELOAD`.
macOS uses `DYLD_INSERT_LIBRARIES` where the process permits injection.
A protected process or a static executable can have a different interception path.
The shim operates at wrapped library calls, independently of the model's description of its intent.

### Files, navigation, and permissions

The shim resolves the supplied path and checks the applicable cached rules.
A matching file denial returns `EACCES` before the real operation.
Related wrappers check directory navigation and permission changes against their projected rules.
A nonmatching path proceeds.
A failed path canonicalization also proceeds through this path.
Selected bootstrap operations, such as random-device access, have explicit exceptions.

File rules are therefore concrete resource controls.
A file deny rule can stop a supported read even when the model wants that file.
A positive task description alone does not create a filesystem allowlist.

### Executables and operands

The execution gate checks command forms, the executable path, and selected path-like arguments.
A matching deny returns `EACCES` before the real exec call.
Supported exec wrappers restore the shim environment for the child.
This extends coverage to descendants that load the library and use the wrapped symbols.
It does not make every process creation API a supervisor query.

### Connections

The `connect` wrapper checks numeric IPv4 or IPv6 destinations.
An allow match takes precedence over a deny match in this path.
A deny match returns `EACCES` before the real connection.
With `ARA_SHIM_NET_ENFORCE` enabled, an unmatched destination also returns `EACCES`.
Armory enables that flag by default for task runs, subject to an operator override.
Without that flag, an unmatched connection is recorded and permitted.
Unix sockets generally proceed, apart from configured adapter redirects.

Hostname rules and numeric destination rules describe different values.
The native connection wrapper sees the resolved IP address.
Network policy preparation must supply the values used at that boundary.

### Handshake and rule state

The shim authenticates its initial connection with the session token.
A loaded, supervised shim denies supported calls when its initial handshake fails.
`ARA_SHIM_FAILOPEN` permits the configured escape from that initial failure behavior.
After a successful handshake, native enforcement uses the cached rule payload.
The current native client has no rule-update receive loop.
A later socket failure does not clear its initialized cache.
Audit transmission and local denial can therefore have different availability.

Armor shadow mode permits matching native operations while recording their would-deny status.
A shadow record is an observation result.

Sources: [handshake](../src/guard/libc_shim/ipc.c#L286), [cached context](../src/guard/libc_shim/ipc.c#L391), [file decision](../src/guard/libc_shim/shim.c#L639), [exec decision](../src/guard/libc_shim/shim.c#L433), [network decision](../src/guard/libc_shim/shim.c#L928), [navigation and permissions](../src/guard/libc_shim/shim.c#L1145).

## 3. Runtime hooks close specific API boundaries

### Node

The Node guard starts through `NODE_OPTIONS` and an import hook.
It exits when necessary session values are absent, the socket is unreachable, or the supervisor rejects its token.
It installs supported filesystem, process, HTTP, socket, and related hooks after a successful handshake.
It can receive updated synchronous rules from the supervisor.

Local filesystem matches reject the operation before the original API call.
Local network matches return an error before the original HTTP or socket operation.
The local network matcher checks allow rules before deny rules.
A local miss generally proceeds with an audit event.
A disconnected supervisor does not turn every local miss into a denial.

Supported asynchronous process wrappers query the supervisor before they start the real child.
A nonallow decision or query failure stops that spawn path.
Synchronous wrappers have different behavior.
The CommonJS synchronous process wrappers record activity and propagate the environment.
Those wrappers do not wait for an asynchronous policy decision.
The ESM synchronous variants apply their local command checks.
The native shim supplies its separate exec checks when loaded.

### Python and Hermes

The launcher adds the Python bootstrap to `PYTHONPATH`.
For a task run, the bootstrap wraps `subprocess.Popen` before the original constructor runs.
It sends an `exec.spawn` query through the transcript client.
A nonallow decision, invalid result, or transport exception raises `PermissionError`.
The task needs protocol version `v1` and a nonempty task identifier for this Python gate.
A Python process that disables site initialization does not import this bootstrap through the normal mechanism.
Other process APIs retain their own interception requirements.

These runtime hooks connect higher policy and task state to actual API decisions.
Their value comes from a verified consumer path, rather than the presence of a policy file alone.

Sources: [Node arming](../src/guard/index.mjs#L33), [filesystem checks](../src/guard/hooks/fs.mjs#L29), [Node process query](../src/guard/hooks/child_process.mjs#L289), [synchronous process wrapper](../src/guard/hooks/child_process.mjs#L475), [Python decision](../src/ara/clients/sitecustomize.py#L83), [Python process wrapper](../src/ara/clients/sitecustomize.py#L182).

## 4. Supervisor policy and the operating contract

A submitted query enters the supervisor's composition path.
The deterministic Bulwark evaluator selects scoped deny, approval, and allow rules.
An unmatched query starts with `allow`.
The Trim gate can then tighten that decision.
Task progress applies to an otherwise permitted target action.
Recognized irreversible danger supplies a separate hard denial.
Approval handling and the active shadow setting produce the final result.
The requesting consumer applies that result.

The full evaluator and local projections have different contracts.
General Bulwark evaluation gives deny groups precedence over allow groups.
Native, Node, seccomp, and proxy connection paths use their network allow-first behavior.
The native network and command projections omit regular-expression rules.
Those projections also omit the evaluator's live counter and expiry state.
Use the [policy reference](BULWARK-SPEC.md) and the selected consumer together when you review a rule.

### Trim gates

The active Trim contributes time windows, stop conditions, tool classification, operating posture, and rate settings.
The composite runtime evaluates those conditions in sequence.
A denied time window or latched stop condition can stop a submitted operation.
Known tool classification can reject active work in a passive engagement.
Unknown classification and necessary approval tokens produce approval decisions in the supervisor path.

The supervisor retains the prior policy decision when no active Trim view exists.
It also retains that decision if Trim evaluation raises an exception.
A configured rate overflow of `deny` can reject the operation.
The rate API can return a queue delay for other callers.
The current supervisor does not sleep for that returned queue delay.
The supervisor also supplies no populated findings summary to this Trim call.
Stop conditions that depend on findings need the relevant supplied state or established latch.

### Approval and shadow behavior

Approval is a decision path with explicit automation settings.
An eligible routine operation can receive an automatic grant after the configured timeout.
An unattended task can instead use model-assisted danger attestation.
A negative danger answer permits that attempt.
A confirmed danger answer or attestation error denies it.
Other pending approvals wait for operator resolution and use deny on timeout.

Armor shadow mode converts final query denials into permitted observations, except for recognized irreversible danger.
This conversion also applies after an approval resolves to deny.
Live enforcement therefore depends on the selected observation setting as well as the rule.

Sources: [query composition](../src/ara/supervisor.py#L781), [Bulwark precedence](../src/ara/bulwark.py#L820), [rule projection](../src/ara/supervisor.py#L517), [Trim integration](../src/ara/supervisor.py#L1304), [approval branches](../src/ara/supervisor.py#L943), [final shadow conversion](../src/ara/supervisor.py#L1027).

## 5. Task progress and channel authority

A task snapshots its checked equipment and persists its action state.
The task gate checks submitted `exec.spawn` and `browser.nav` target actions.
Missing task state, changed equipment hashes, or a blocked task produces a denial.
A task can also owe a commit, branch update, or verification pass.
That debt holds the next applicable queried target action.

Accepted actions spend the configured budgets and can create the next debt state.
A valid record or transition changes that state through its specific channel.
Control-plane actions have explicit exemptions.
Network connections and HTTP requests do not spend these action budgets.
They use their resource policy paths separately.
The retired native raw exec-count channel gate is disabled in the supplied guard environment.

The channel dispatcher adds another task boundary.
It rejects unregistered channels and channels outside the active chunk's `tool_surface`.
It checks task transition requirements before execution.
The supervisor assigns the `agent` caller to agent channel requests.
The channel then checks the caller and body schema before it changes a store.
An agent cannot gain operator channel permission by naming itself as an operator in the body.

Channel restrictions govern the typed interface.
They do not independently remove every shell executable from the host.
The applicable execution guards retain that separate responsibility.

Sources: [task query selection](../src/ara/task_action_gate.py#L35), [budget and debt decision](../src/ara/task_session.py#L173), [channel dispatch](../src/ara/supervisor.py#L1549), [caller and schema checks](../src/ara/runtime/channels/base.py#L90), [disabled raw tally](../src/ara/supervisor.py#L2092).

## 6. Model transport, guidance, and observation

The session proxy checks applicable CONNECT network rules before it opens a tunnel.
A matched denial returns HTTP `403` before upstream connection.
Configured intercepted model hosts then use the supported request and reply inspection paths.
A request denial supplies a synthetic reply before the upstream request.
A reply denial can replace supported response content.
Other encrypted tunnels receive the connection decision without model payload inspection.

Traffic must actually use the proxy for these controls.
`ARA_SKIP_MITM` disables that proxy path.
The launcher also supplies `NO_PROXY` entries for selected local addresses.
A client that ignores proxy settings needs its other applicable network controls.
Some proxy evaluation errors retain forwarding behavior.

The Python OpenAI SDK wrapper offers a separate application path.
A request deny returns a synthetic completion before the original SDK call.
A supported rewrite changes its message or system fields.
During task runs, unavailable connections and transport errors return denial through the transcript client.
Outside task runs, those failures return allow.
Malformed or mismatched replies can also reach that client's allow return.
`ARA_PYINTERCEPT_DISABLE` disables SDK interception while retaining the separate subprocess integration.

ABSOLUTE and the chunk brief give the model its identity and procedure.
They can explain scope, expected channels, permitted exits, and record obligations.
These instructions guide reasoning.
They do not themselves deny a file or network syscall.

Shadow requests, canary detection, transcripts, and evidence capture supply observation.
The Node fetch capture hook sends transcript messages but discards their decisions before the original fetch.
That hook records model traffic; it is not a request denial gate.
The OpenClaw gateway tap is another protocol-specific observation path.
Its presence does not change the default worker.

Sources: [proxy CONNECT](../src/ara/mitm_proxy.py#L775), [request denial](../src/ara/mitm_proxy.py#L1073), [Python SDK application](../src/ara/clients/sitecustomize.py#L435), [transcript failure branches](../src/ara/clients/transcript.py#L122), [Node fetch observation](../src/guard/hooks/fetch.mjs#L83), [ABSOLUTE rewrite](../src/ara/mitm_proxy.py#L1594).

## 7. Accepted output and operator review

Captured bytes, typed facts, leads, and findings have separate roles.
A tool result can enter evidence without becoming a finding.
A draft finding can remain unresolved while its explanation or support is incomplete.
Finding validation checks the applicable references, evidence content, and lifecycle conditions.
Content checks use bounded matching and derivation rules.
They are concrete acceptance checks for those conditions.

The sign-off channel permits the operator caller.
It checks the finding state and sign-off validation before it appends `signed_off`.
A failed check rejects that transition.
Reports consume signed-off findings.
This prevents an unaccepted draft from entering the normal report path.
It does not change an operation that already occurred.

The operator also selects equipment, changes policy, inspects approvals, and controls finding dispositions.
These interfaces give the run an external review path.
Their own access depends on host permissions and control-plane authentication.

Sources: [channel caller restriction](../src/ara/runtime/channels/base.py#L90), [operator sign-off](../src/ara/runtime/findings/channels.py#L486), [sign-off validation](../src/ara/runtime/findings/validator.py#L662), [evidence and review](evidence.md).

## Worked attempt: read a protected file

Assume this global rule is active:

```text
## global
deny fs /workspace/private/**
```

Assume the process loads the native shim and completes its handshake.
Its cached payload contains this path denial.
Armor shadow mode is off.
The worker calls a wrapped open function for `/workspace/private/credentials.json`.

1. The wrapper calls `enforce_open` before the real libc function.
2. The gate obtains the initialized context and resolves the path.
3. `ara_match_path` returns the matching rule identifier.
4. The gate records a deny and sets `errno` to `EACCES`.
5. The wrapper returns failure without the real open operation.

The model's request does not override that branch.
The same rule has a different consumer path for a supported Node file API.
That hook applies its local denial before the original JavaScript API.

Source: [native open branch](../src/guard/libc_shim/shim.c#L639).

## Worked attempt: connect outside the network allowlist

Assume a loaded task shim receives these projected network rules:

```text
## global
allow net 127.0.0.1:*
deny net *:*
```

Assume shadow mode is off and the worker calls wrapped `connect` for `198.51.100.77:443`.
The IP is an example address used to explain the decision.

1. The wrapper extracts the numeric address and port.
2. The loopback allow rule does not match.
3. The broad deny rule matches.
4. The wrapper returns `EACCES` before the real connection.

An installed seccomp filter supplies another decision for its selected network syscalls.
Its supervisor checks projected allow rules before projected deny rules.
A matching deny produces an `EACCES` notification response.
A connection to the permitted loopback address instead passes the allow-first branch.

Sources: [native network branch](../src/guard/libc_shim/shim.c#L1049), [seccomp decision](../src/ara/supervisor.py#L1154), [notification deny](../src/ara/seccomp_notif.py#L234).

## Worked attempt: continue without the necessary commit

Assume a Hermes task has spent its action budget.
Its persisted phase is `commit_required`.
Bulwark and Trim otherwise permit the proposed next process launch.
Armor shadow mode is off.

1. The Python task wrapper sends `exec.spawn` before the real `Popen` call.
2. The supervisor reaches the task gate after its prior allow decision.
3. The task gate loads the existing run.
4. `record_action` returns `TASK_COMMIT_REQUIRED`.
5. The supervisor returns deny.
6. The Python wrapper raises `PermissionError` before the child starts.
7. A permitted commit channel can update the task state.

An additional network connection alone does not spend this action budget.
Network scope remains the responsibility of the applicable network policy path.

Sources: [Python pre-spawn decision](../src/ara/clients/sitecustomize.py#L83), [task gate](../src/ara/task_action_gate.py#L35), [commit debt branch](../src/ara/task_session.py#L196).

## Deployment preparation and runtime proof

Contained launch prepares a session HOME, temporary directories, profile state, and supported guard environment.
It records the contained layout in a manifest.
This directory preparation is not a filesystem namespace or chroot.
Absolute resource access still uses host permissions and the applicable guards.
Linux contained launch checks that its shim file has an ELF header.
That file check does not prove every target process loads the library.

Task preflight checks equipment, active-testing permission, and the end time when present.
It also attempts to converge the selected Trim policy section.
Those checks establish task preparation.
They do not install or prove a seccomp filter.

Armory also starts periodic Hermes gateway reconciliation unless disabled.
The deployed guard can kill detected uncontained Hermes processes and restart the configured contained gateway.
Repeated strikes can hold the gateway off.
This is delayed process remediation with deployment dependencies.
It is separate from the per-operation resource decisions described above.

Inspect the relevant session records when you verify an active control path.
Use handshake records for guard startup and seccomp attachment records for notification setup.
Use operation decisions for the specific attempted boundary.
Use task records for debt and equipment continuity.
Use finding history for acceptance and review.

Sources: [contained directories](../src/ara/containment.py#L73), [shim screening](../src/ara/launch.py#L42), [task preflight](../src/ara/task_preflight.py#L25), [Hermes reconciliation](../src/ara/hermes_guard.py#L197), [Armory guard startup](../src/ara/armory/app.py#L4564).

## Why the layers belong together

A resource rule gives a concrete answer at a supported operation boundary.
A Trim adds engagement conditions that a path match cannot express.
Task state adds continuity and progress obligations.
The model envelope supplies the knowledge needed to choose useful permitted work.
Evidence connects observed content to later reasoning.
Record validation and operator review determine which results become accepted output.

This composition supports capable work with explicit permission and inspectable results.
Each layer retains its own activation, coverage, and decision contract.
The [architecture](architecture.md), [system codex](codex.md), and [end-to-end workflows](end-to-end.md) connect those contracts to the complete product.
