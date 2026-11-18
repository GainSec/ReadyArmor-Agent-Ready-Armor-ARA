# Interfaces and controls

ARA provides operator controls, observation views, and worker integration interfaces.
Select the interface for the action you intend to perform.
Use the same state directory for Armory and the workers it supervises.

## Choose an interface

| Interface | Role | Reads or changes |
| --- | --- | --- |
| Armory browser | Operator control and review | Equipment, Forge, engagement settings, task starts/stops, approvals, policy, and results |
| Courtyard browser | Live observation and available operator decisions | Session activity, pending decisions, events, and relationships |
| CLI | Local operator or automation | Command-specific inspection and state changes under host file permissions |
| Curses debug viewer | Terminal observation | Structured debug events and record details |
| HTTP form actions | Operator-side automation | The same implemented state transitions used by browser forms |
| HTTP tails and SSE | Observation client | Debug tails, audit rows, and model-turn update counts |
| MCP over stdio | Worker or tool client | Advertised authoring tools, task channels, or pending Temper candidates |
| Unix-socket IPC | Worker integration | Operation decisions, runtime events, model interaction, and typed channel calls |

Use Armory with trusted operators.
The handler accepts administrator and viewer credentials for protected GET routes.
Its protected POST action routes require the administrator role.
These token roles are separate from local CLI file permissions and worker channel caller roles.

## Armory and Courtyard

Start the browser interface with a dedicated state directory:

```sh
ara armory --state-root .ara --bind 127.0.0.1 --port 4770
```

Open the address printed at startup.
Sign in through `/login` with the appropriate Armory credential.
The state directory contains `armory-token` and `armory-viewer-token`.
The server accepts the `t` query parameter or its `armory_token` cookie.

| Browser route | Use |
| --- | --- |
| `/status` | Inspect equipment and session status. |
| `/courtyard` | Inspect active work, decisions, and relationships. |
| `/engagement` | Prepare and drive the task workflow. |
| `/armors`, `/weapons`, `/trims`, `/temper` | Inspect and select equipment through the available controls. |
| `/mats`, `/forge` | Manage source material and capability authoring. |
| `/bulwark`, `/rhema`, `/absolute` | Inspect resource rules and supported model-interaction controls. |
| `/engagement/evidence`, `/engagement/spoils` | Review captured material and task output. |
| `/engagement/findings`, `/engagement/leads` | Inspect findings and investigation work. |
| `/debug` | Inspect combined diagnostic streams. |
| `/settings` | Examine operator settings and credential-related controls. |
| `/docs` | Read runtime product topics. |

Several older paths redirect into the current interface.
`/approvals`, `/audit`, and `/graph` lead to Courtyard views.
`/baselines` leads to Armors.
The `/sessions` index also redirects to Courtyard.
Session detail and export routes remain available.

For a normal task, prepare the loadout and engagement context before selecting Start.
Armory uses Hermes and its persistent task loop by default.
The task view exposes the selected equipment, logs, structured progress, and available stop controls.
See [task procedures](tasks.md) and [evidence review](evidence.md).

Source: [GET routes](../src/ara/armory/app.py#L2087),
[credential roles](../src/ara/armory/app.py#L1640),
and [task start](../src/ara/armory/app.py#L1084).

## Local CLI workflows

Use `ara --help` for all command groups.
Use `ara COMMAND --help` for a group's arguments.
Use the help for its subcommand when a group has several actions.

| Group | Operator task |
| --- | --- |
| `armory` | Start the browser control surface. |
| `launch`, `claude`, `openclaw` | Start the supplied worker or an explicit adapter. |
| `armor`, `weapon`, `trim` | Select and inspect task equipment. |
| `forge` | Create Weapons and Packs, inspect coverage, calibrate retrieval, and review revisions. |
| `approval` | Inspect and resolve pending approval requests. |
| `bulwark`, `overrides`, `blackholes` | Inspect or manage their policy records. |
| `session`, `status`, `engagement` | Inspect stored sessions and current engagement state. |
| `evidence`, `verify-audit` | Examine stored artifacts and record integrity. |
| `tool` | Inspect and manage tool classification. |
| `canary`, `baseline` | Manage marked values and reference-run comparisons. |
| `debug-log` | Print debug events or open the live diagnostic viewer. |

Examples for an existing state directory:

```sh
ara status --state-root .ara
ara armor list --state-root .ara
ara weapon --state-root .ara current
ara trim status --state-root .ara
ara approval list --state-root .ara
ara session list --state-root .ara
```

Resolve an identified approval through the explicit action:

```sh
ara approval respond APPROVAL_ID deny --state-root .ara
```

The approval command also supports `allow`, session actions, and persistent actions with an optional TTL.
Examine the target and lifetime before selecting that action.
`ara session resume SESSION_ID --state-root .ara` checks resumability and prints the canonical continuation command.

For an identified session directory, inspect captured artifacts and audit records:

```sh
ara evidence list .ara/sessions/SESSION_ID
ara evidence verify .ara/sessions/SESSION_ID
ara verify-audit SESSION_ID --state-root .ara
```

The CLI reads and writes local state through the launching user's file permissions.
It does not use the Armory HTTP token as a separate authorization layer.

Source: [operator command registration](../src/ara/admin_cli.py#L444)
and [inspection commands](../src/ara/inspect_cli.py#L325).

## Terminal diagnostics

The diagnostic viewer is a shipped curses interface for structured debug events.
It provides observation and record inspection.
Use Armory or the relevant CLI command for task administration.

```sh
ara debug-log --state-root .ara --follow
ara debug-log --state-root .ara --no-curses --limit 50
ara debug-log --state-root .ara --component supervisor --level error
```

`--follow` opens the live curses view.
`--no-curses` prints records and exits.
Without `--follow`, the command also uses printed output.
Filters include component, event, task, session, text, and level.
`--log` selects a specific debug file.

In curses, use `j` or Down to select the next record.
Use `k` or Up to select the previous record.
Press Enter to inspect a record, then `q` or Escape to return.
Press `q` or Escape in the main view to exit.

Source: [CLI options](../src/ara/cli.py#L235)
and [curses viewer](../src/ara/debug_viewer.py#L69).

## HTTP clients and form actions

Armory exposes its browser routes through an HTTP server.
Mutations use form fields and redirect responses.
The request body parser reads URL-encoded fields; upload routes use their multipart handling.
Use the implemented route and field contract for each action.

| Method and route | Fields or result |
| --- | --- |
| `POST /login` | `password` and optional `next`; sets the token cookie and redirects. |
| `GET /healthz` | Public HTML health response. |
| `GET /debug/tail` | `session` and stream offsets; returns JSON text. |
| `POST /engagement/start` | Task form fields such as `worker_cmd`, `prompt`, and `task_depth`; this task path enforces containment and runs preflight. |
| `POST /engagement/stop` | Optional `session_id`; requests task stop and redirects. |
| `POST /weapons/equip` | `weapon_id`; selects the specified sealed Weapon and redirects. |
| `POST /weapons/unequip` | Releases the selected Weapon and redirects. |
| `POST /approvals/{id}/respond` | `action`, optional `ttl`, and optional `resolved_url`; resolves the identified request. |

Set `ARA_ARMORY_TOKEN` to the credential intended for the operation.
This read request uses the supported query parameter:

```sh
export ARA_ARMORY_URL="http://127.0.0.1:4770"

curl -fsS --get "$ARA_ARMORY_URL/debug/tail" \
  --data-urlencode "t=$ARA_ARMORY_TOKEN" \
  --data-urlencode 'session=SESSION_ID'
```

An operator client can submit a form action with its administrator credential:

```sh
curl -i -X POST "$ARA_ARMORY_URL/weapons/equip?t=$ARA_ARMORY_TOKEN" \
  --data-urlencode 'weapon_id=reviewed-method'
```

The action returns a redirect with its result in the next page's flash fields.
It does not return a general JSON command response.
Invalid or missing POST credentials receive HTTP 401.
A viewer credential on a protected action receives HTTP 403.
Login and logout have their own credential and cookie handling.

Source: [body parsing](../src/ara/armory/app.py#L1668),
[POST role checks](../src/ara/armory/app.py#L2861),
[Weapon selection](../src/ara/armory/app.py#L3134),
and [approval response](../src/ara/armory/app.py#L3448).

## Live observation through SSE

SSE means Server-Sent Events.
Armory supplies two authenticated GET streams:

| Route | Stream content |
| --- | --- |
| `/events` | New audit and policy-hit rows from recent sessions, with rendered HTML, session prefix, and verdict fields. |
| `/rhema/events` | A `new_since_open` count when recent transcript files grow. |

Both streams use `text/event-stream` and heartbeat comments.
The audit stream positions existing files at their current end when the connection opens.
Use stored session records to read earlier history.
The Rhema stream signals new turns; the page or client retrieves the desired detail separately.

```sh
curl -N --get "$ARA_ARMORY_URL/events" \
  --data-urlencode "t=$ARA_ARMORY_TOKEN"
```

Source: [audit SSE](../src/ara/armory/app.py#L1951)
and [Rhema SSE](../src/ara/armory/app.py#L1871).

## Worker MCP bridges

MCP means Model Context Protocol.
ARA's MCP servers use line-delimited JSON-RPC over standard input and output.
They provide structured tools for a worker or compatible tool client.
They do not provide Armory administrator controls.

### Forge and task bridge

```sh
ara forge-mcp
```

The server reads `ARA_SOCKET` and `ARA_SESSION_TOKEN` from its prepared session environment.
It forwards channel calls through `ForgeChannelClient`.
The selected mode determines the advertised tool catalog.

| Mode | Main tool family |
| --- | --- |
| `forge` | Source reading and Weapon extraction tools. |
| `pack` | Vocabulary and Pack proposal tools. |
| `reconcile` | Existing vocabulary conflict and merge tools. |
| `workflow` | Source reading and chunk proposal tools. |
| `task` or `engagement` | Facts, leads, findings, evidence lookup, and chunk transition tools. |

`ARA_FORGE_MCP_MODE` selects a recognized mode.
An Armory Hermes loop also selects task mode through its runtime environment.
A call outside the advertised mode returns a tool-unavailable error.
Request `tools/list` before constructing a tool call.

A minimal catalog request sequence is:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"example-client","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

Send each object as one input line.
Use `tools/call` with the returned tool name and its advertised argument schema.

Source: [mode selection](../src/ara/clients/forge_mcp_server.py#L668),
[catalog handling](../src/ara/clients/forge_mcp_server.py#L817),
and [mode checks](../src/ara/clients/forge_mcp_server.py#L850).

### Temper proposals

```sh
ara temper-mcp
```

This server uses `ARA_STATE_ROOT`, then `ARA_STATE_DIR`, then `.ara` to locate its state.
It reads the active Tailor session and source material through its supported tools.
It validates submitted control candidates and records pending proposals or rejection information.
Coverage-summary calls update the authoring coverage record.
Operator review and later equipment actions determine which material becomes active policy.

Source: [Temper tool dispatch](../src/ara/clients/temper_mcp_server.py#L514)
and [state selection](../src/ara/clients/temper_mcp_server.py#L588).

## Worker session IPC

ARA uses newline-delimited JSON over a local Unix socket.
The launcher supplies `ARA_SOCKET`, `ARA_SESSION_TOKEN`, and session context to its integrations.
The supervisor handles operation queries, events, model-interaction messages, and typed channel calls.

A guard handshake sends `hello` with the session token and process metadata.
The supervisor returns `hello_ack` with an accept or reject verdict.
Accepted guard handshakes also receive projected rules for supported local decisions.
The supplied channel SDK sends its hello when a token is present, without waiting for that acknowledgment.
Use each supplied integration's request contract.

An operation query identifies the proposed action:

```json
{
  "type": "query",
  "seq": 1,
  "op": "fs.read",
  "path": "/workspace/example.txt",
  "pid": 1234
}
```

The supervisor returns a decision associated with `seq`.
An adapter must wait for the applicable permission result before dispatching its operation.
A query requests a decision; it does not perform the file read or start an arbitrary command.
Event messages record observations through a separate path.

A typed record uses `channel_call`:

```json
{
  "type": "channel_call",
  "seq": 2,
  "channel": "evidence.search",
  "body": {"query": "configuration"}
}
```

The response type is `channel_call_response`.
It carries the sequence number, an `ok` flag, and a result or error information.
The supervised worker dispatch assigns the agent caller role.
Operator-only channels, such as finding sign-off, use their separate permission checks.
Available channels also depend on the installed engagement and dispatch path.

For Python worker clients, use `ara.clients.forge_channel_sdk.ForgeChannelClient`.
Its `call` method matches replies by sequence number and raises `ChannelCallError` for a rejected call or transport failure.
The native guard and Node client have their own supplied operation-query integrations.
See [integration choices](integrations.md) before implementing another consumer.

Source: [socket server](../src/ara/ipc.py#L15),
[message dispatch](../src/ara/supervisor.py#L584),
[hello handling](../src/ara/supervisor.py#L718),
[typed dispatch](../src/ara/supervisor.py#L1549),
and [Python channel client](../src/ara/clients/forge_channel_sdk.py#L60).

## Continue work through stored records

Equipment, task state, approvals, session events, transcripts, evidence, and findings remain in the selected state directory.
A later operator can inspect those records through Armory and the CLI.
The inspection does not depend on retaining the original model conversation in memory.
Use the administrator credential for HTTP actions and host file permissions for local CLI actions.
These mechanisms provide shared operational continuity rather than named-user role management.
