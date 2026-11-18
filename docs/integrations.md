# Workers, models, and tool integrations

ARA separates the agent worker, inference provider, host tools, and reusable method.
Select each part for the work it performs.
The current interfaces connect those choices to ARA policy, session state, and review records.

## Agnostic by design

| Choice | Integration point | Retained ARA records |
| --- | --- | --- |
| Reasoning model | Selected worker configuration or stateless endpoint settings | Method, engagement, evidence, findings, and review history |
| Worker runtime | Hermes, an explicit adapter, or the generic launcher | The supervised session and its applicable controls |
| Domain method | Source materials, Packs, alphabets, and forged Weapons | Reviewed procedure structure and source references |
| Engagement | A separately reviewed Trim | Reusable method and standing hardening |
| Host tool | Existing executable used through the selected worker | Applicable operation decisions, captures, and results |
| Operator interface | Browser, CLI, or implemented HTTP action | The shared state directory and operational records |

The model provider supplies inference.
The worker manages a conversation and calls tools.
A method artifact supplies reusable domain structure.
Changing one choice does not automatically configure the others.

For example, a Hermes run can use its configured model service while retaining the same Weapon and Trim.
A different worker can receive the selected method through its integration.
The active guard, model-interaction, and channel paths determine the controls for that worker.

## Select the worker

Armory's normal task path uses **Hermes**.
The generic launcher starts the supplied executable.
Claude Code and OpenClaw have explicit adapter commands.

| Worker path | Entry point | ARA preparation |
| --- | --- | --- |
| Armory Hermes task | Start through `/engagement` | Equipment preflight, task records, contained profile, persistent loop, and worker bridges |
| Generic executable | `ara launch -- COMMAND` | Session supervisor, selected policy, guard environment, and optional contained directories |
| Claude Code | `ara claude` | Selected executable and ARA-owned Claude profile environment |
| OpenClaw | `ara openclaw` | Selected executable and ARA-owned OpenClaw profile environment |

Install and configure the selected worker before launch.
Use the [interface guide](interfaces.md) for operator controls and programmatic entry points.
Use [Control in Depth](control-in-depth.md) for the active consumer contracts.

Source: [CLI dispatch](../src/ara/cli.py#L346),
[Armory task launch](../src/ara/armory/app.py#L1084),
and [generic launch](../src/ara/launch.py#L88).

### Hermes

The Armory task controller prepares the contained worker lifecycle, task context, and persistent loop.
The supplied task profile mounts structured task tools through the Forge MCP bridge.
Hermes selects its model and provider through its own configuration.
The bundled profiles use a local LM Studio endpoint by default.

A separately supervised Hermes gateway can use:

```sh
ara launch --state-root .ara --contain --contain-profile hermes-gateway -- hermes gateway run
```

The gateway's messaging and channel interfaces belong to Hermes.
The generic gateway launch and Armory's persistent task controller are separate entry points.

### Claude Code

The adapter locates `claude` and builds the worker command from the supplied arguments.
Its profile environment sets `CLAUDE_HOME` and `CLAUDE_CONFIG_PATH`.

```sh
ara claude --state-root .ara --contain
ara claude --help
```

Use `--claude-bin` or `ARA_CLAUDE_BIN` to select the executable.
Use `--profile` to select its ARA profile.
Use `--weapon` to select a sealed Weapon for that launch.
Arguments after `--` pass to the worker.

Source: [Claude adapter](../src/ara/workers/claude_code.py#L59).

### OpenClaw

The adapter sets `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`, and its worker-specific heartbeat timeout.
It also sets `OPENCLAW_SANDBOX=0` for this integration.
ARA supplies its own supported supervision paths around the worker.

```sh
ara openclaw --state-root .ara --contain -- gateway run
ara openclaw --help
```

Use `--openclaw-bin` or `ARA_OPENCLAW_BIN` to select the executable.
Use `--profile` to select its ARA profile.
The exec approval proxy and gateway tap provide additional OpenClaw integration paths when their configuration and protocol match.
OpenClaw supplies its own messaging gateways and channel connectivity.

Source: [OpenClaw environment](../src/ara/workers/openclaw.py#L37)
and [command construction](../src/ara/workers/openclaw.py#L67).

### Generic executables and custom workers

Use `ara launch` for a host executable without a dedicated adapter.
Use `--` to separate ARA options from the worker command.

```sh
ara launch --state-root .ara --contain -- python3 -c "print('ARA worker')"
```

This starts the supplied process under the generic session launcher.
Armory task preflight and its persistent Hermes lifecycle belong to the separate task controller.
A custom integration can use the supplied Unix socket to request operation decisions or call available typed channels.
The [interface reference](interfaces.md#worker-session-ipc) describes those messages.

## Configure model inference separately

A worker's model settings select the inference service and model identifier.
The ARA adapter prepares the worker process; it does not select every provider setting for that program.
Configure the chosen worker's supported provider route before starting its task.

Stateless Forge and Temper model calls have their own endpoint settings:

| Setting | Effect |
| --- | --- |
| `ARA_FORGE_MODEL_ENDPOINT` | Select the stateless Forge chat-completion endpoint. |
| `ARA_TEMPER_MODEL_ENDPOINT` | Select the stateless Temper endpoint; also supplies Forge's fallback endpoint setting. |
| `ARA_MODEL_HEALTH_URL` | Select the model-health observation URL. |

An explicit endpoint argument takes priority for the stateless call.
The default chat-completion URL is `http://127.0.0.1:1234/v1/chat/completions`.
The bundled Hermes profile base URL is `http://127.0.0.1:1234/v1`.
These settings do not replace a worker's own provider configuration.
The health URL selects an observation request rather than reconfiguring model inference.

Supported model mediation also depends on the provider envelope and active request path.
For a custom provider, examine its request parser, guard route, and capture behavior.
Use Rhema and the session records to inspect the resulting model interaction.
See [model configuration](configuration.md#local-model-service) and [Rhema and model envelopes](codex.md#rhema-absolute-and-shadows).

Source: [stateless Forge call](../src/ara/forge/loop_driver.py#L966),
[stateless Temper call](../src/ara/temper_driver.py#L433),
and [health observation](../src/ara/armory/debug_views.py#L48).

## Use existing host tools

ARA can supervise a worker that uses installed host programs.
The selected tool does not need to become a new model or a new domain Pack.
Its relevant operation paths meet the consumers loaded for that session.

| Consumer | Relevant tool activity |
| --- | --- |
| Native shared library | Supported libc file, executable, and numeric network destination calls |
| Node guard | Supported JavaScript resource hooks and asynchronous operation queries |
| Python integration | Supported SDK capture and task subprocess decisions |
| Linux seccomp notification | Selected network syscalls after successful filter installation |
| Supervisor | Applicable policy, engagement, task, classification, and approval decisions submitted to it |

The tool registry records tool identity and flag patterns.
Applicable Trim and supervisor paths use those classifications during operation decisions.
The model still needs the method, tool availability, and correct invocation arguments for useful work.

Guard coverage follows the actual call path.
A custom worker should verify which native, language, model, and record interfaces it uses.
The [host control guide](ready-armor.md#control-on-the-ordinary-host) traces those relationships.

## Host platform paths

The source includes Linux and macOS launch behavior.
Build the native guard for the host and use an executable that permits the selected loading mechanism.

| Host path | Mechanism |
| --- | --- |
| Linux native guard | `LD_PRELOAD` loads the shared library into supported dynamic processes. |
| macOS native guard | `DYLD_INSERT_LIBRARIES` and the selected namespace settings request library injection. |
| Additional Linux network gate | Enabled settings, supported x86-64/kernel screening, and successful seccomp installation. |
| Worker integration | Node or Python consumers apply only when that runtime and consumer are active. |

The Linux notification filter covers selected `connect`, destination-bearing `sendto`, and `io_uring_setup` cases.
A successful platform screen does not establish successful installation.
Prepared home and cache directories provide session organization rather than a filesystem namespace.
See [Control in Depth](control-in-depth.md) for decisions and failure behavior.

## Worker tool interfaces

MCP and session IPC provide structured worker access.
They have different roles from the operator's Armory and local CLI controls.

| Bridge | Use | Selection |
| --- | --- | --- |
| `ara forge-mcp` | Advertised Forge authoring or task channel tools | Prepared socket environment and the selected MCP mode |
| `ara temper-mcp` | Source lookup, coverage summaries, and pending Temper candidates | Active Tailor session in the selected state directory |
| `ForgeChannelClient` | Typed channel calls from a Python worker client | Local session socket and available channel context |
| Supplied guard IPC | Operation queries, runtime events, and supported model interaction | Guard environment and its request contract |

The Forge MCP catalog changes by mode.
Task mode exposes facts, leads, findings, evidence lookup, and chunk transitions.
Temper calls record candidates before operator selection and later policy activation.
Use `tools/list` and its schema instead of assuming an operator administration surface.

Source: [Forge MCP modes](../src/ara/clients/forge_mcp_server.py#L668),
[Temper tool dispatch](../src/ara/clients/temper_mcp_server.py#L514),
and [channel client](../src/ara/clients/forge_channel_sdk.py#L60).

## Retain work across changes

The state directory preserves equipment, task records, approvals, captures, findings, and review history.
These records can remain useful when the worker conversation, model configuration, or operator changes.
The new operator can inspect the stored work and select the appropriate continuation.

```sh
ara session list --state-root .ara
ara session resume SESSION_ID --state-root .ara
```

The resume command checks the stored session and prints a continuation command.
Review the current engagement window, selected equipment, and pending obligations before continuing work.
Use the same state directory when you want the same task and evidence records.
The [task guide](tasks.md) explains the persistent lifecycle.
