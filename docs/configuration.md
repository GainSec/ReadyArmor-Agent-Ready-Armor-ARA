# Configuration

## State directory

Use one state directory for Armory and the workers that it supervises.
The `--state-root` option selects the directory for commands that provide this option.
Commands otherwise use `ARA_STATE_ROOT` where supported, then `.ara` in the current directory.

```sh
export ARA_STATE_ROOT="$PWD/.ara"
ara armory --state-root "$ARA_STATE_ROOT"
```

The state directory contains policy, equipment, sessions, approvals, certificates, and audit records.
Back up this directory with its access permissions.
Keep the state directory outside version control.

## Runtime options

| Variable | Function |
|---|---|
| `ARA_PYTHON` | Select the Python interpreter for `bin/ara` |
| `ARA_STATE_ROOT` | Select the shared state directory |
| `ARA_OPENCLAW_BIN` | Select the OpenClaw executable |
| `ARA_CLAUDE_BIN` | Select the Claude Code executable |
| `ARA_HEARTBEAT_TIMEOUT_S` | Set the supervisor heartbeat timeout in seconds |
| `ARA_APPROVAL_TIMEOUT_S` | Set the model proxy approval timeout |
| `ARA_SKIP_MITM` | Disable the model proxy when set to a nonzero value |
| `ARA_CHUNK_INHIBITION_ENFORCE` | Activate configured relaxation of eligible non-scope rules |
| `ARA_SHIM_NET_ENFORCE` | Deny unmatched native IP connections when the loaded shim applies live enforcement |
| `ARA_SHIM_FAILOPEN` | Permit supported shim calls after an initial supervisor handshake failure |
| `ARA_SECCOMP_NET_ENFORCE` | Request selected Linux network syscall filtering for contained launch |
| `ARA_SECCOMP_FAILOPEN` | Permit unmatched IP destinations and destination-read failures in the installed notification path |
| `ARA_PYINTERCEPT_DISABLE` | Disable Python SDK interception while retaining the separate subprocess integration |
| `ARA_HERMES_GUARD_DISABLE` | Disable periodic Armory Hermes gateway reconciliation when set to `1` |

The launcher supplies `ARA_SOCKET` and `ARA_SESSION_TOKEN` to contained workers.
Use these values through the supplied integrations.

## Armory network settings

The default address is `127.0.0.1`.
The default port is `4770`.

Use `--bind` to select an address.
Use `--port` to select a port.
Use `--tls-cert` and `--tls-key` to supply a TLS certificate and private key.
Use `--behind-proxy` for a deployment behind a reverse proxy.
Use `ara armory --help` to examine the complete option list.

## Local model service

The bundled Hermes profiles select the `lmstudio` provider.
Their default base URL is `http://127.0.0.1:1234/v1`.
Configure the profile's `model.base_url` and `model.default` for the selected service and model.

Stateless Forge and Temper calls use the same local service by default.
These variables select their request endpoints and the model health check:

```sh
export ARA_FORGE_MODEL_ENDPOINT="http://127.0.0.1:1234/v1/chat/completions"
export ARA_TEMPER_MODEL_ENDPOINT="http://127.0.0.1:1234/v1/chat/completions"
export ARA_MODEL_HEALTH_URL="http://127.0.0.1:1234/v1/models"
```

Forge uses its own endpoint setting before the Temper endpoint setting.
An explicit endpoint argument has priority over these settings.

Generated Trims retain loopback access for local control services.
Add explicit provider and resolver rules for the selected deployment.
The bundled armor and weapon policies retain their listed public provider rules.
Examine the effective policy before a worker connects to another service.

## Hermes service paths

The Hermes gateway guard derives its home directory from the runtime user.
Its service PATH includes common user tool directories and system executable directories.
It does not copy Armory's potentially restricted PATH.

| Variable | Function |
| --- | --- |
| `ARA_ROOT` | Select a source checkout whose `src` directory supplies ARA imports. |
| `HERMES_BIN` | Select the Hermes executable path. |
| `ARA_HERMES_UNIT_PATH` | Set the complete PATH for the user service. |
| `ARA_UNDICI_MODULE_PATH` | Select the optional Node runtime's bundled `undici` directory. |

Without `ARA_ROOT`, the service uses the installed ARA package location.
The default Hermes executable is `$HOME/.local/bin/hermes`.
The default Node dependency is `$HOME/.npm-global/lib/node_modules/openclaw/node_modules/undici`.
These executable and dependency path overrides expand a leading `~`.
Forge and Temper MCP profiles inherit the launcher's `PYTHONPATH`.

## Model certificates

The model proxy uses OpenSSL to create its certificate authority and host certificates.
Contained workers receive the certificate configuration from the launcher.
Keep the certificate authority key in the protected state directory.
Use the session logs to diagnose certificate trust failures.

## Native guard

Build the guard from the source checkout:

```sh
make -C src/guard/libc_shim
```

The output is `build/libara_shim.so`.
Linux uses `LD_PRELOAD` for this guard.
macOS uses `DYLD_INSERT_LIBRARIES` where the process permits library injection.
Host restrictions can prevent library injection into protected processes.

## Effective task controls

Armory selects Hermes and the contained task profile by default.
It sets `ARA_SHIM_NET_ENFORCE` and `ARA_SECCOMP_NET_ENFORCE` to `1` unless the operator environment overrides them.
Actual enforcement still needs the corresponding consumer to load or install successfully.
A successful platform screen does not prove seccomp installation.
Contained directories do not create a filesystem namespace.
Use the [Control in Depth guide](control-in-depth.md) to inspect activation, failure behavior, and operation records.
