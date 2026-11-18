# The Ready Armor approach

Ready Armor starts with the resources that useful work can affect.
A capable agent can read files, start processes, contact services, and produce convincing explanations.
Each capability needs permission at its relevant boundary.
Each accepted result needs support and review.

ARA applies this idea through separate resource, policy, task, model, and record interfaces.
The model reasons about the objective.
The surrounding system decides supported operation permission and record acceptance.
An operator selects that system's equipment, engagement, rules, and review decisions.

This gives the work several concrete control points.
A protected file can remain unread when a loaded guard denies its open call.
A task can stop its next queried process launch until it records the previous result.
A draft finding can remain outside a report until its sign-off conditions pass.
These outcomes arise from different code paths.
Their composition is the Ready Armor approach.

The default Armory task worker is Hermes.
The generic launcher starts the operator-supplied command.
Claude Code and OpenClaw are explicit integration choices.

## The operational problem

A useful agent can choose actions that its operator did not list individually.
That flexibility helps with investigation, source analysis, and procedures that need judgment.
The operator still needs a defined scope, visible decisions, and a reviewable result.

ARA connects the method to those operating conditions.
The operator supplies material, selects equipment, defines engagement permission, and sets review policy.
The agent reasons and acts through the selected runtime interfaces.
The system retains method structure, task state, evidence, and decisions outside the current model conversation.

This creates a practical unit of work: a reviewed method applied under a specific authorization.
The method can remain stable while the target, window, or hardening changes.
The result can remain available after the conversation ends.

## Control on the ordinary host

ARA's normal launch starts the worker directly as a host process.
It does not need a separate VM, container, or operating-system sandbox to supply its operation controls.
The default Armory worker is Hermes.
The launcher prepares the worker environment, loads the applicable guards, and connects the process to a session supervisor.

This keeps the tools near the resources they already use.
The control question becomes concrete: which supported call is occurring, which policy applies, and which component can stop it?
The answer comes from the operation consumer and task state.

### Native resource calls provide a separate control path

The native shim is a shared library loaded into the worker process.
Linux uses `LD_PRELOAD`; supported macOS launches use `DYLD_INSERT_LIBRARIES`.
These loader settings place ARA wrappers at supported libc interfaces.
A native file or executable call can therefore reach policy checks without the agent explicitly calling an ARA SDK.

For example, the open wrapper canonicalizes the requested path and checks the cached path rules.
A matching live deny records the hit and returns `EACCES` before the real open.
The execution wrapper checks the executable and applicable argument paths before the original execution call.
The connection wrapper checks the numeric destination through its network rule path.
These decisions operate on the attempted resource operation.
They do not need the model to classify its own action as forbidden.

The supervisor supplies the native rule projection during the initial guard handshake.
The native consumer applies that cached projection.
Supported Python and asynchronous Node paths can instead request supervisor decisions with additional task context.
A model-proxy decision has another role: it inspects supported request and reply content.
These paths use different inputs and act at different boundaries.

Source: [loader injection](../src/ara/launch.py#L381),
[native file denial](../src/guard/libc_shim/shim.c#L637),
[native execution checks](../src/guard/libc_shim/shim.c#L433),
and [native connection denial](../src/guard/libc_shim/shim.c#L1060).

### Carry controls into helper work

A worker often starts a shell, interpreter, or analysis program.
ARA's supported `execve` and `execvpe` wrappers prepare the child environment before the original execution call.
Their environment helper restores the shim's `LD_PRELOAD` entry.
Ordinary environment inheritance also carries the session settings.
Language-specific subprocess handling supplies another integration path for supported workers.

This extends control into helper executions that use those paths.
Actual coverage depends on guard loading, the executable, platform behavior, and the inherited environment.
The [Control in Depth guide](control-in-depth.md) identifies the active consumers and their precise contracts.

Source: [child loader propagation](../src/guard/libc_shim/shim.c#L322),
[execve wrapper](../src/guard/libc_shim/shim.c#L601),
and [execvpe wrapper](../src/guard/libc_shim/shim.c#L625).

### Add selected kernel checks on Linux

The optional Linux seccomp path installs its filter in the worker before execution.
It requests kernel notification for `connect` and destination-bearing `sendto` calls.
It also denies `io_uring_setup` through the installed filter.
The listener can permit or deny the selected network syscall before it proceeds.
This adds a control path for those syscalls below native-library interposition.

Activation needs the selected settings, a supported platform, and successful filter installation.
The launcher can continue with its other controls when installation fails.
This is a selected network restriction, rather than a complete process-isolation boundary.

Source: [filter decisions](../src/ara/seccomp_notif.py#L95),
[filter installation](../src/ara/seccomp_notif.py#L129),
and [launch activation](../src/ara/launch.py#L447).

### Separate session organization from operation permission

ARA's contained session prepares a separate home, temporary directories, caches, and worker configuration.
It copies selected material into that environment.
The code calls this a contained workspace or sandbox directory tree.
Those names describe session preparation; the directories do not create a kernel filesystem namespace.
The loaded consumers and applicable policy determine resource denials.

Higher controls add information that a path or destination cannot express.
Task state knows whether the run owes a record or transition.
Typed channels know the caller and record contract.
Finding checks know the cited evidence and acceptance requirements.
Operator sign-off supplies the separate acceptance decision.

This is why the complete control path matters.
Host operation checks, runtime integration, task state, model guidance, and record acceptance each supply a different part of the result.
The operator can deploy the controls around existing host tools and inspect which paths are active for the selected worker.

Source: [session environment](../src/ara/containment.py#L73),
[task action checks](../src/ara/task_action_gate.py#L33),
[channel validation](../src/ara/runtime/channels/base.py#L90),
and [finding sign-off](../src/ara/runtime/findings/channels.py#L486).

## The distinctive composition

ARA's distinctive design is the connection between capability creation and controlled capability use.
Forge prepares the method that the supervised task later receives.
Engagement policy defines where and when that method may operate.
Evidence and finding records carry the result into a separate review path.
Reviewed revisions can then improve the next method artifact.

| Connection | Implemented mechanism | Practical value |
| --- | --- | --- |
| Source to method | Manifest identities, typed extraction, synthesis, and seal | Inspect the source basis and structure before reuse. |
| Method to engagement | Weapon plus separate Armor, Trim, and Temper selections | Reuse domain expertise while reviewing permission for each engagement. |
| Method to task state | Chunk definitions, valid exits, activation records, and task obligations | Inspect the current phase and unfinished recording work. |
| Policy to operation | Applicable guard, supervisor, and resource-consumer decisions | Deny a supported call at its actual decision boundary. |
| Observation to accepted claim | Evidence Binder, finding validators, operator sign-off, and report selection | Review captured material before accepting the conclusion. |
| Experience to later work | Reforge proposals, component differences, and reviewed Temper equipment | Retain improvements as inspectable artifacts. |

These mechanisms address related questions without giving them the same answer.
A resource permission does not complete a procedure.
A captured response does not approve a finding.
A new method artifact does not authorize a new target.
The operator can examine each transition through its own records.

Source: [seal](../src/ara/forge/phases/seal.py#L266),
[task action gate](../src/ara/task_action_gate.py#L33),
[channel contract](../src/ara/runtime/channels/base.py#L90),
[finding sign-off](../src/ara/runtime/findings/channels.py#L486),
and [Reforge differences](../src/ara/forge/reforge/proposal.py#L110).

## Intelligence, capability, and authority

Intelligence is the model's ability to reason about the task.
Capability is the set of tools, procedures, and resources available for the work.
Authority is permission to use that capability under the selected conditions.

A model can propose a command without receiving permission to execute it.
It can describe a finding without satisfying the evidence and review requirements for sign-off.
ARA places these decisions in explicit runtime and record interfaces.

| Responsibility | Owner or mechanism | Persistent expression |
|---|---|---|
| Task intent | Operator | Engagement intake and task objective |
| Standing identity | Armor | Armor metadata, policy, and ABSOLUTE variants |
| Procedure | Weapon | Sealed knowledge and chunk graph |
| Engagement permission | Trim | Scope, authorization, windows, and operating controls |
| Reusable hardening | Temper | Reviewed doctrine and policy package |
| Immediate operation decision | Applicable guard and supervisor path | Policy decision and audit record |
| Accepted output | Validation plus operator review | Signed-off finding and report |

The operator can change one responsibility without rebuilding all the others.
For example, a new client engagement can reuse an existing weapon with a different Trim.
A revised hardening package can apply to several task methods.

## Control in Depth

Control in Depth starts at operation boundaries and builds toward accepted output.
It combines controls that use different information and produce different effects.
A resource gate knows the path or destination.
A task gate knows what the current run owes.
The model knows the supplied procedure and conversation.
A finding validator knows the record and its cited evidence.
The operator supplies permission and final review.

### Start with what the operation can affect

Files, executable calls, and network destinations are concrete boundaries.
A loaded native shim can reject matching supported libc operations before the original call.
Its file and execution denials return `EACCES`.
Its network wrapper can reject a numeric destination before `connect` proceeds.
An installed Linux seccomp filter adds kernel decisions for its selected network syscall forms.

These controls do not depend on the model agreeing that the operation is inappropriate.
They depend on the actual intercepted call, applicable policy, and active enforcement setting.
That is the purpose of putting authority around the capability.

### Connect policy to a real consumer

A policy rule becomes effective through a consumer that applies it.
The Python task wrapper queries the supervisor before its original `Popen` call.
Supported asynchronous Node process wrappers also wait for a decision.
Other Node resource hooks use local projected rules.
The native shim uses its initial cached rule payload.
The Linux notification handler uses its projected network decision path.

These consumers have distinct defaults and coverage.
General Bulwark evaluation uses deny-first precedence and permits unmatched queries.
The network connection consumers use their allow-first paths.
Armory enables native unmatched-network denial by default for task runs, subject to an operator override.
Linux notification setup additionally needs supported platform screening and successful installation.

The relevant unit of control is therefore specific:
resource, consumer, rule, activation, decision, and recorded outcome.
The [Control in Depth guide](control-in-depth.md) traces each of these elements to its actual source branch.

### Add engagement and task meaning

A path rule cannot express every operating condition.
The Trim contributes engagement windows, activity posture, stop conditions, tool classification, and rate settings.
The supervisor composes applicable Trim decisions after Bulwark.
This composition can hold or reject an otherwise permitted operation.

A task adds persistent continuity.
Its checked equipment hashes identify the selected loadout.
Its phase records whether it owes a commit, branch update, or verification pass.
The task gate can reject the next applicable queried process launch or browser navigation.
Network connections use their separate resource rules and do not spend those action budgets.

These mechanisms help keep an investigation attached to its approved context and recorded progress.
They also make a stalled run explainable through explicit state.
The operator can inspect the debt or equipment mismatch instead of interpreting the conversation alone.

### Give the model useful direction

The Armor supplies standing identity and instructions.
The Weapon supplies a reusable method, vocabulary, knowledge, chunk graph, and expected outputs.
The Trim supplies engagement context.
The Temper adds reviewed hardening and doctrine.
ABSOLUTE and supported request rewrites compose that material for the model.

This guidance helps the model select useful work within the selected method.
The chunk brief identifies available channels and valid exits.
The channel dispatcher separately rejects a channel outside the active surface.
Model guidance and channel rejection therefore serve related but distinct purposes.
The instruction explains the interface; the dispatcher applies its contract.

### Preserve observation without confusing it with permission

Transcripts and evidence records make activity available for review.
Shadow requests provide alternate model output for comparison.
Canary detection records marked values in supported replies.
The Node fetch capture hook records transcript activity without applying its returned decisions.
These observation paths provide useful information without independently stopping the original resource operation.

Armor shadow mode also changes operation behavior.
It permits selected would-deny results for observation.
Approval automation can grant eligible routine operations after a configured timeout.
Unattended task approvals can use model-assisted danger attestation.
These are explicit operating policies that the operator can inspect.
They must be included when the operator assesses the active control path.

### Control accepted output separately

A successful tool call is an observation, not an accepted conclusion.
Evidence stores captured content.
Facts and leads represent structured knowledge and unresolved investigation.
Findings connect a claim, support, impact, and remediation.
Their validators examine the applicable schema, references, content, and lifecycle conditions.

Operator sign-off has a separate caller permission and validation path.
A failed sign-off check leaves the finding unaccepted.
The report path consumes signed-off findings.
This makes acceptance an explicit state transition outside the agent's ordinary channel authority.

### Review and reuse the complete chain

Source review and seal create a reusable method artifact.
Preflight checks the current task equipment and engagement permission.
Operation gates mediate supported activity.
Task state keeps progress connected to records.
Model guidance supplies the method and context.
Record checks and operator review determine accepted output.
Reforge and Temper review carry selected improvements into later work.

The result is a repeatable relationship between capability, permission, observation, and acceptance.
The operator can reuse a method without treating every engagement as the same authorization.
The operator can also improve hardening without replacing the model's useful reasoning or the procedure's domain knowledge.

## Controls outside the agent's ordinary work

The supervisor owns operation decisions and typed channel dispatch.
The operator owns configuration changes and review actions through the control interface.
Guard IPC identifies the session.
Channel contexts assign the caller role used by channel permission checks.
For example, the finding sign-off channel permits the operator caller.
Agent channel messages receive the agent caller role.

Containment also prepares separate worker configuration and temporary directories.
Seed armor rules protect selected credentials, worker configuration, and operator resources.
The exact resource coverage follows the host, guard, and active policy configuration.

The practical consequence is clear ownership.
The agent performs the assigned work through the available interface.
The surrounding system controls the boundaries and acceptance rules.

## Source-based capability

ARA carries approved method knowledge forward as equipment.
A source element becomes an extraction event with an identity and citation.
Synthesis places those events into typed component groups.
Seal writes the equipment files, vocabularies, policy, chunks, search index, and hash metadata.

This supports review before execution.
The operator can examine what the procedure contains and which source elements were excluded.
A later task receives the selected artifact and engagement context.
Changes can be compared against a predecessor through Reforge.

Model-assisted authoring remains a model operation.
Repeatable parsers, explicit schemas, and stored artifacts provide stable checkpoints around it.
The useful unit of reuse is the reviewed artifact.

### The authoring agent also works through ARA

The default Forge extraction path starts Hermes through `ara launch --contain --contain-profile forge`.
Its Hermes profile supplies the Forge MCP bridge and the structured authoring tools.
The resulting extraction events feed the manifest coverage and synthesis stages.
Capability creation therefore uses ARA's launch and record machinery before the resulting method reaches a later task.

The equipment names identify separate authoring jobs.
Blacksmith and Hammer support Weapon creation.
Codex and Pen support Pack creation.
Tailor Robes and Needle support Temper candidates.
The operator can inspect the resulting method, domain definitions, or hardening proposal before later use.

This is useful when the important knowledge belongs to your team.
You can start with your procedures and source material, inspect what the authoring workflow produces, and retain the reviewed result.

Source: [contained Forge invocation](../src/ara/forge/loop_driver.py#L185)
and [Forge profile](../src/ara/seed/hermes-profiles/forge/config.yaml#L50).

## How method knowledge becomes operating structure

A task method needs several kinds of knowledge.
Procedures explain actions and expected results.
Concepts define the categories and relationships used during the work.
Evidence shapes describe the records that an analysis step expects.
A workflow defines which phase comes next and which structured interfaces it exposes.

Forge keeps those representations distinct.
Procedure extraction becomes crumbs.
Conceptual extraction becomes shapes and can supply typed alphabets.
Fact-shape extraction supplies declared record structure.
Chunk templates and definitions supply the procedure graph.
The sealed artifact can include those components alongside policy text and a search index.

This structure gives the runtime specific things to consume.
The model can retrieve relevant method components.
A chunk can describe its available channels and valid exits.
The chunk runtime can check a submitted transition against the current exit table and caller conditions.
Finding handling can read the weapon's categories, rubrics, remediation templates, and field expectations.
These interfaces turn selected method knowledge into task inputs and checkable record contracts.

A citation identifies the source element associated with a component.
Seal checks citation presence for its configured component groups and checks the configured manifest coverage threshold.
Its explicit force option can record a coverage exception while citation checks still apply.
Review examines the content and intended use of the resulting artifact.
The citation and coverage checks give that review stable reference points.

Source: [synthesis](../src/ara/forge/phases/synthesize.py#L223),
[alphabet projection](../src/ara/forge/alphabets/projector.py#L255),
[coverage checks](../src/ara/forge/phases/seal.py#L107),
[citation checks](../src/ara/forge/phases/seal.py#L119),
and [chunk transitions](../src/ara/runtime/chunks/runtime.py#L202).

### Structure attention, progress, and reuse

ARA uses explicit state for several functions that a conversation alone would leave implicit.
A chunk names the current procedural phase.
An activation identifies a period of work and evidence use.
Facts preserve structured assertions.
Leads preserve questions that still need investigation.
Finding records preserve the claim and its review state.

The phase brief helps direct the model's attention.
Task obligations separately track whether the run owes a commit, branch update, or verification pass.
For applicable queried actions, the task gate can withhold permission until the obligation is resolved.
Checked loadout hashes detect a change to the equipment recorded for the run.
These records let an operator diagnose the current condition through stored state.

Chunk inhibition provides another explicit setting.
It describes the active chunk's per-layer inhibition profile.
Configured enforcement can relax eligible non-scope rules marked as relaxable.
Missing profile data resolves to strict inhibition.
The [Control in Depth guide](control-in-depth.md) explains the consumer and activation conditions.

The useful design principle is external task structure.
The model reasons within the supplied method; the runtime keeps its own progress and acceptance records.
A team can inspect those records, improve the method, and reuse the reviewed artifact with another conversation.

Source: [activation records](../src/ara/runtime/activation.py#L56),
[task progress](../src/ara/task_session.py#L173),
[loadout continuity](../src/ara/task_session.py#L415),
and [inhibition profiles](../src/ara/runtime/chunks/inhibition.py#L50).

## Keep organizational knowledge as reviewed artifacts

An engagement can reveal a useful procedure, a missing branch, or a hardening rule worth retaining.
ARA gives those observations a path into later equipment.
A proposed revision names its predecessor and records additions, removals, and modified components.
Approval determines which proposed revision proceeds through the new seal path.
Temper provides separate reviewed hardening that can apply across methods.

This retains knowledge in files and records that the operator can inspect.
It does not depend on changing model weights.
A later operator can reuse the method, inspect its source references, and compare its revisions.
A new engagement still receives its own permission and operating conditions.

Retrieval has its own evaluation procedure.
An operator can provide representative queries and expected component identifiers to the calibration command.
That check examines the artifact's retrieval behavior for those cases.
The review can therefore cover both method content and the queries expected during use.

Source: [revision proposal](../src/ara/forge/reforge/proposal.py#L143),
[revision approval](../src/ara/forge/reforge/driver.py#L107),
and [Temper equipment](../src/ara/tempers.py#L247).
See [retrieval calibration](forge.md#calibrate-retrieval) for the query format and command.

## Why the equipment roles matter

An armor represents a durable operating identity.
A weapon represents a method for a class of work.
A Trim represents one engagement's permission and context.
A Temper represents reviewed hardening across tasks.
A Pack supplies reusable definitions from which capabilities are made.

Combining these roles creates a task loadout.
An active task snapshots that loadout.
If its checked equipment changes, the task can block further target operations.
Policy reload, equipment selection, and task continuity are therefore separate operations.

## Agnostic design preserves the useful parts

ARA separates several choices that an agent deployment often makes together.
The inference provider supplies a model response.
The worker manages the conversation and tools.
The method supplies domain knowledge and workflow structure.
The engagement supplies permission and operating context.
The interface supplies the operator's control or observation surface.

| Replacement | Supported route | State that can remain available |
| --- | --- | --- |
| Model or inference service | Configure the selected worker or the stateless model endpoints. | Equipment, task records, evidence, and accepted findings. |
| Worker runtime | Use Hermes, an explicit Claude Code/OpenClaw adapter, or the generic command launcher. | Selected ARA policy and session records through that integration. |
| Domain method | Forge a Weapon with the selected Pack's vocabulary and workflow definitions. | Standing Armor, reviewed hardening, and the equipment review process. |
| Engagement target | Create and review a separate Trim. | The method artifact and reusable vocabulary. |
| Operator or conversation | Read the persistent task, approval, evidence, and review records. | The method and the recorded work beyond one conversation. |
| Control interface | Use Armory, local CLI commands, or the implemented HTTP action paths. | The shared state directory and its records. |

This makes replacement a reviewable integration decision.
A new worker still needs its own valid launch and control path.
An existing host tool reaches the checks provided by its loaded native or language-specific consumers.
The current implementation has Linux and macOS loader paths.
Selected Linux network syscalls can receive the additional seccomp path when installation succeeds.

Armory browser access is for trusted operators.
The interface does not supply a new permission by itself.
An Armory POST needs the administrator role; a local CLI command uses host file access.
A worker MCP tool uses its advertised mode and the associated channel or proposal path.
A session channel call receives the worker caller context through its dispatch path.
These are distinct interfaces over related records.

The practical benefit is continuity with explicit control ownership.
A team can keep its reviewed method when the model configuration changes.
It can inspect earlier work when another operator takes over.
It can use browser controls for review and CLI commands for repeated local operations.
The [interface reference](interfaces.md) identifies the available controls and observation paths.
The [integration guide](integrations.md) distinguishes worker selection from provider configuration.

Source: [worker dispatch](../src/ara/cli.py#L386),
[Claude Code adapter](../src/ara/workers/claude_code.py#L59),
[OpenClaw adapter](../src/ara/workers/openclaw.py#L67),
[HTTP roles](../src/ara/armory/app.py#L1640),
[POST actions](../src/ara/armory/app.py#L2861),
and [MCP mode selection](../src/ara/clients/forge_mcp_server.py#L668).

## Practical examples

### Repeated client assessments

Use the same reviewed web assessment weapon for several engagements.
Create a separate Trim for each target and operating window.
Apply the standing armor and reviewed hardening package.
Review evidence and findings under the same output requirements.

### One method across two engagements

Consider a team that has an approved API assessment procedure and earlier assessment reports.

1. Deposit the approved material and identify the selected source elements.
2. Use Forge to extract procedures, concepts, expected records, and workflow material.
3. Examine the draft, source references, coverage, and policy before seal.
4. Equip the reviewed Weapon with the standing Armor and reviewed Temper.
5. Create a Trim for the first target, authorization, operating window, and limits.
6. Start the supervised Hermes task and inspect its task and control records.
7. Record observations and investigate leads through the available structured interfaces.
8. Examine each finding's evidence, explanation, impact, and remediation before sign-off.
9. Use a new Trim when the same method serves a different engagement.
10. Propose a method revision when the work reveals a useful change.

The method, engagement permission, evidence, and accepted result have separate identities throughout this process.
That separation makes reuse practical and changes reviewable.
The team retains a method artifact and its supporting records beyond either model conversation.

### Internal source research

Deposit architecture material, source files, and approved procedures in an engagement workspace.
Select a domain Pack suitable for the expected record types.
Create a weapon that carries the relevant procedure and vocabulary.
Use the resulting evidence and unresolved leads to guide further review.

### Operational change windows

Define the operating window and applicable stop criteria through the engagement context.
Use the procedure's chunk graph to separate preparation, execution, and review phases.
Record the decisions and observed results through the same task lifecycle.

The value comes from composition.
ARA connects a reusable method to explicit permission, runtime decisions, evidence, and accepted output.

Implementation: [_on_query](../src/ara/supervisor.py#L781), [call](../src/ara/runtime/channels/base.py#L90), [set_active_armor](../src/ara/armors.py#L371), [load_armor](../src/ara/armors.py#L436), [equip_policy](../src/ara/weapons.py#L209), [unequip_policy](../src/ara/weapons.py#L252), [_maybe_install_engagement](../src/ara/supervisor.py#L1514), [check_active_trim_for_task](../src/ara/task_preflight.py#L25), [_start_task_session](../src/ara/armory/app.py#L1084), [record_action](../src/ara/task_session.py#L173), [record_commit](../src/ara/task_session.py#L273), [current_loadout_matches](../src/ara/task_session.py#L415), [FindingSignOffChannel](../src/ara/runtime/findings/channels.py#L486), [validate_for_signoff](../src/ara/runtime/findings/validator.py#L662), [seal](../src/ara/forge/phases/seal.py#L266), [validate_manifest_coverage](../src/ara/forge/phases/seal.py#L107), [validate_citation_integrity](../src/ara/forge/phases/seal.py#L119).
