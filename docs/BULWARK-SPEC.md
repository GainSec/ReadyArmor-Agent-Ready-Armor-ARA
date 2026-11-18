# Bulwark policy reference

Bulwark stores rules in `<state-root>/bulwark.md`.
A rule selects an action, a layer, and a match expression.
Section headers select the sessions to which a rule applies.

## Grammar

```text
<action> [<modifier> ...] <layer>[:<sublayer>] <match> [# comment]
```

```text
## global
deny fs **/.ssh/**
approval exec /usr/bin/curl
allow fs /workspace/**

## armor: research
deny net blocked.example.com
approval x3 then:deny ask "restricted material"

## config
counter_exhaust_default: deny
```

## Actions

| Action | Meaning |
|---|---|
| `deny` | Stop the matching operation |
| `approval` | Enter the applicable approval path |
| `allow` | Permit the matching operation |
| `rewrite` | Describe a request rewrite |
| `shadow_rewrite` | Describe an alternate request for comparison |

The deterministic evaluator selects `deny`, `approval`, and `allow` rules.
The parser also accepts rewrite actions.
ABSOLUTE projection and the proxy control the live rewrite path.

## Layers

| Layer | Match expression |
|---|---|
| `fs` | File path |
| `nav` | Directory path |
| `perm` | Optional `op=<operation>`, then a path |
| `exec` | Optional `argv=<pattern>`, then an executable path |
| `net` | Host, with an optional port |
| `url` | Optional HTTP method, then a URL |
| `env` | Environment variable name |
| `ask` | Text in a model request |
| `reply` | Text in a model reply |
| `acp` | Agent communication method name |
| `oc:<tool>` | Target of a named worker tool |

`oc` selects the complete worker tool family.
Common tool names include `read`, `write`, `edit`, `bash`, `glob`, `grep`, `webfetch`, and `websearch`.
A worker integration must send tool queries for these rules to operate.

## Scope

| Header | Application |
|---|---|
| `## global` | All sessions |
| `## armor: research` | Sessions with the research armor |
| `## armor: alpha+beta` | Sessions with either named armor |
| `## armor: *-research` | All armors except research |
| `## global -armor: research` | Global scope with the named exclusion |

## Precedence

Bulwark evaluates these groups in order:

1. Global deny rules.
2. Armor deny rules.
3. Global approval rules.
4. Armor approval rules.
5. Global allow rules.
6. Armor allow rules.
7. The default decision.

File order selects the first applicable rule within a group.
The default evaluator decision is `allow` when no rule matches.
Individual interception paths can apply additional restrictions.
A broad allow rule does not cancel a matching deny rule in this general evaluator.

Connection consumers use a separate allow-first contract.
The native shim, Node local matcher, seccomp listener, and proxy CONNECT path examine applicable network allows before network denies.
The native shim additionally needs `ARA_SHIM_NET_ENFORCE` for unmatched connection denial.
An installed seccomp listener denies unmatched IP destinations unless its fail-open setting is enabled.
Native command and network projections omit regex rules and live counter or expiry state.
See [Control in Depth](control-in-depth.md) for exact consumer coverage.

## Patterns and modifiers

`*` matches characters within one path segment.
`**` matches across path segments.
`?` matches one character within a segment.
The `regex:` prefix selects a regular expression.

| Modifier | Function |
|---|---|
| `ttl 30s` | Expire the rule after thirty seconds |
| `ttl 15m` | Expire the rule after fifteen minutes |
| `ttl 2h` | Expire the rule after two hours |
| `ttl 7d` | Expire the rule after seven days |
| `x3` | Set a maximum of three matches |
| `then:deny` | Deny after the match count expires |
| `then:allow` | Permit after the match count expires |
| `then:approval` | Request approval after the match count expires |
| `then:fallthrough` | Continue to the next rule after the match count expires |
| `highlight` | Mark the rule for prominent display |

## Verify policy

1. Edit `bulwark.md` in the state directory.
2. Examine parser errors.

   ```sh
   ara bulwark verify --state-root .ara
   ```

3. Examine the resulting rule list.

   ```sh
   ara bulwark list --state-root .ara
   ```

Use `ara bulwark migrate --dry-run` to examine migration from legacy policy files.
Use the same state directory for the migration and the supervised sessions.
