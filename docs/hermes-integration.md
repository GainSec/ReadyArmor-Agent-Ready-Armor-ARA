# Agent integrations

## Hermes

Hermes provides the persistent task loop used by Armory task runs.
The controller manages the contained worker lifecycle and task context.
Install and configure Hermes before you start a Hermes run.

A generic contained gateway launch uses:

```sh
ara launch --state-root .ara --contain --contain-profile hermes-gateway -- hermes gateway run
```

The generic launcher supervises the supplied process.
Armory task controls provide the persistent task lifecycle.

## Claude Code

The Claude Code launcher prepares a separate profile and the ARA control environment.

```sh
ara claude --state-root .ara --contain -- chat
```

Use `--claude-bin` to select a specific executable.
Use `--profile` to select the ARA profile.
Use `--weapon` to equip a sealed weapon for that session.

## OpenClaw

The OpenClaw launcher prepares an ARA-owned worker profile.

```sh
ara openclaw --help
```

Use `--openclaw-bin` to select the executable.
Use `--contain` to prepare session directories.
The exec approval proxy and gateway tap support additional OpenClaw control paths.
The gateway tap depends on its configuration and worker protocol.

## Model Context Protocol

MCP means Model Context Protocol.
ARA provides stdio servers for Forge channels and Temper proposals.

```sh
ara forge-mcp
ara temper-mcp
```

Run the Forge server inside a contained session with the supplied socket and token.
The Temper server records pending candidates for the active Tailor session.
Operators select the candidates before those candidates change active armor policy.

## Custom workers

Use `ara launch` for an executable without a dedicated adapter.
Use `--` to separate ARA options from the worker command.
A custom tool integration can send queries through the session socket.
See the [interface reference](interfaces.md) for the query protocol.
