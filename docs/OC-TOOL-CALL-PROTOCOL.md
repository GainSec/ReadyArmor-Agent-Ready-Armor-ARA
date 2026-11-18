# Interface reference

## Command groups

Use `ara --help` for all commands.
Use `ara COMMAND --help` for a command's arguments.

| Group | Function |
|---|---|
| `launch`, `claude`, `openclaw` | Start supervised workers |
| `armory` | Start the operator interface |
| `armor`, `weapon`, `trim` | Manage equipment |
| `forge` | Create weapons and packs |
| `bulwark` | Verify and inspect policy |
| `approval` | Examine and resolve approval requests |
| `overrides`, `blackholes` | Manage compatibility policy entries |
| `session`, `status` | Examine session state |
| `evidence`, `verify-audit` | Examine evidence and audit integrity |
| `canary`, `baseline` | Manage observation aids |
| `engagement` | Examine engagement state |
| `tool` | Manage tool classification |
| `debug-log` | Read structured debug events |

## Armory routes

Armory serves HTML pages through its HTTP server.
The token parameter `t` authenticates normal browser requests.
Form actions use the application's supplied controls and redirect responses.

| Route | Page |
|---|---|
| `/healthz` | Health response |
| `/status` | Equipment and session status |
| `/courtyard` | Current activity |
| `/armors` | Armor catalog |
| `/weapons` | Weapon catalog |
| `/forge` | Forge sessions |
| `/bulwark` | Policy rules |
| `/rhema` | Model traffic |
| `/approvals` | Redirect to Courtyard approval requests |
| `/audit` | Redirect to Courtyard live activity |
| `/baselines` | Redirect to Armors |
| `/graph` | Redirect to Courtyard relationships |
| `/docs` | Product topics |

The server defines route handling in `src/ara/armory/app.py`.

## IPC tool queries

Contained workers receive `ARA_SOCKET` and `ARA_SESSION_TOKEN`.
The socket uses newline-delimited JSON messages.
Use the supplied IPC client for the session authentication handshake.
Send the tool query before the worker dispatches the operation.

```json
{
  "type": "query",
  "seq": 1,
  "op": "oc.tool_call",
  "tool": "read",
  "target": "/workspace/example.txt",
  "pid": 1234
}
```

The supervisor returns a decision with the matching `seq` value.

```json
{
  "type": "decision",
  "seq": 1,
  "verdict": "deny",
  "reason": "Matched policy rule",
  "rule_id": "example-rule",
  "touched": "/workspace/example.txt"
}
```

1. Wait for the matching decision.
2. For `allow`, dispatch the operation.
3. For `deny`, return an operation error.
4. For `approval`, wait for the resolved decision.
5. Record the result through the appropriate runtime channel.

The IPC implementation is in `src/ara/ipc.py` and `src/guard/ipc.mjs`.
