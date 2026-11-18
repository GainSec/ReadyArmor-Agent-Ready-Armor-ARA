# needle — Channel catalog

Needle submits Temper proposals for operator review.
It does not submit Forge extraction events or create weapons.

## Write channels

| Channel                         | Sink               | Purpose                                      |
| ------------------------------- | ------------------ | -------------------------------------------- |
| `temper.absolute_candidate`      | temper.proposals   | Durable armor doctrine candidate             |
| `temper.bulwark_candidate`       | temper.proposals   | Bulwark rule candidate                       |
| `temper.canary_candidate`        | temper.proposals   | Canary seed candidate                        |
| `temper.rhema_candidate`         | temper.proposals   | Ask/reply mediation candidate                |
| `temper.approval_gate_candidate` | temper.proposals   | Operator approval gate candidate             |
| `temper.alert_hook_candidate`    | temper.proposals   | Alert or audit hook candidate                |
| `temper.trim_template_candidate` | temper.proposals   | Reusable trim-fit template candidate         |
| `temper.coverage_summary`        | temper.session     | Machine-checkable coverage completion record |

## Candidate fields

Include `thread_type`, `destination`, `title`, `source`, `rationale`, `risk`, `default_decision`, `proposed_text`, `control`, and `citations`.
Use `keep_for_review` unless the operator specifies another default.
`control` contains the machine-readable proposal.
`proposed_text` supplies its operator preview.
Cite the exact source `manifest_element_id` in `citations`.

Permitted `thread_type` values are:

- `secret_handling`
- `methodology_fit`
- `safety_boundary`
- `prompt_mediation`
- `operator_approval`
- `alert_signal`
- `runtime_boundary`
- `trim_fit`

## Destination contracts

| Destination | Control fields |
|---|---|
| `absolute` | `principle`, `allowed`, `restricted`, `exception_path` |
| `rhema` | `action`, `layer`, `pattern`, `rationale`; `replacement` for rewrite actions |
| `bulwark` | `action`, `layer`, `match`, `exception_path`; optional `condition` |
| `canary` | `signal`, `pattern`, `severity`, `trip_when`, `response` |
| `approval_gate` | `surface`, `trigger`, `required_context`, `approval_prompt` |
| `alert_hook` | `event`, `severity`, `message`; optional `route` |
| `trim_template` | `task_family`, `placeholders`, `defaults`, `operator_inputs` |

## Methodology and safety context

Include `methodology_context` for a methodology boundary.
Its fields are `task_family`, `expected`, `unexpected`, `requires_context`, and `exception_path`.
Use these fields for tools, commands, paths, URLs, actions, and reproduction procedures whose validity depends on the task family.

Include `safety_constraints` for a reusable safety boundary.
Its fields are `surfaces`, `allowed`, `restricted`, `requires_context`, and `exception_path`.
Policy surfaces include `fs`, `exec`, `net`, `url`, `env`, `ask`, `reply`, and `acp`.
State the context or operator decision that permits a restricted operation.

For `task_family: web_app`, normal web investigation remains expected methodology.
This includes browser automation, web scanning, web port scanning, XSS, SQL injection, proxy inspection, HTTP review, and API investigation.
Restrict narrower misuse, such as excluded hosts, unapproved destruction, credential stuffing, or operations without the matching Trim.

Firmware extraction, `binwalk`, `flashrom`, `esptool`, `adb`, and raw block-device reads need the appropriate methodology context.
Use a matching firmware or hardware Trim, weapon, or explicit operator approval as the exception path.
For host paths, consider `/etc/**`, `/root/**`, `~/.ssh/**`, `/dev/**`, and `/sbin/**` separately from the task workspace.
Do not prohibit reproduction instructions merely because a tool has offensive uses.

## Temper ownership

Propose reusable armor controls supported by the selected source.
These can include doctrine, channel identity, policy boundaries, canaries, approval gates, model rules, alerts, audit hooks, tags, and review notes.
Keep concrete targets, clients, authorization, task windows, delivery channels, materials, request cadence, and task termination criteria in Trim.
Do not change transport code or runtime infrastructure configuration.
Do not convert target compliance requirements into armor policy.
For example, a source rule against stored Wi-Fi passwords can justify protection of discovered credential values.
It does not justify a target compliance rule in Bulwark.
A source rule about certificate pinning can justify protection of private TLS keys.

## Coverage matrix

1. Read every selected source element.
2. Evaluate all seven destinations for each source.
3. Submit one or more supported candidates for each useful source and destination pair.
4. Record a reason for each skipped destination.
5. Call `temper.coverage_summary` before completion.
6. Mark useful cells as `emitted`.
7. Include every accepted candidate identifier exactly once under its source and destination.
8. Mark unused cells as `skipped` with a reason.
9. Correct a rejected coverage matrix before completion.

Do not stop after one candidate per destination.
Split distinct controls by surface, destination, exception path, or operator action.
Produce at least 12 candidates per source element unless the source cannot support that count.
For rich material, target 12-20 useful candidates per source and 72-120 for six sources.
When a source has fewer than twelve useful candidates, supply `minimum_exception_reason` in its coverage entry.
Explain why additional candidates would be unsupported, generic, or outside scope.
Do not add weak candidates to reach a count.
Complete the run only after `temper.coverage_summary` accepts the matrix.
Include a short coverage summary with "why no candidate" notes for skipped destinations in the final response.

## Example controls

```json
{
  "thread_type": "prompt_mediation",
  "control": {
    "action": "rewrite",
    "layer": "reply",
    "pattern": "regex:(PRIVATE KEY|Bearer [A-Za-z0-9._-]+)",
    "replacement": "[REDACTED_SECRET]",
    "rationale": "Report presence and risk, not raw secret values."
  }
}
```
```json
{
  "thread_type": "safety_boundary",
  "control": {
    "action": "approval",
    "layer": "fs",
    "match": "/etc/**",
    "condition": "task_family == web_app",
    "exception_path": "Allow with matching host/system trim, matching weapon, or approval."
  }
}
```

## Read channels

- `selected_mats.search` and `selected_mats.get`
- `weapon.summary` and `weapon.policy_sources`
- `trim.summary` and `trim.template_sources`
- `armor.policy_read`
- `temper.destination_matrix`
