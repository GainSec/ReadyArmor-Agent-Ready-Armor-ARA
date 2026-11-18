# Equipment and loadout management

ARA separates identity, capability, engagement context, and hardening.
A complete task loadout combines Armor, Weapon, Trim, and Temper.
A Pack supplies definitions used during capability creation.

## Armor

An armor supplies persistent identity, policy scope, metadata, and ABSOLUTE instruction variants.
Its files reside under `<state-root>/armors/<armor-name>/`.
The active marker selects the armor for policy evaluation, request composition, and task preparation.

```sh
ara armor list --state-root .ara
ara armor show ARMOR_NAME --state-root .ara
ara armor equip ARMOR_NAME --state-root .ara
```

Replace `ARMOR_NAME` with an available name.
Use `ara armor create --help` for creation arguments.
Use the export and import commands to transfer a selected armor package.
Changing armor attempts to release the prior armor's active Trim.

## Weapon

A weapon supplies a sealed method and its materialized runtime data.
Forge creates the weapon from source material and typed extraction.
The artifact can contain knowledge, chunks, alphabets, resources, policy, a channel catalog, search data, and seal metadata.

```sh
ara weapon --state-root .ara list
ara weapon --state-root .ara current
ara weapon --state-root .ara equip WEAPON_ID
```

Replace `WEAPON_ID` with a sealed weapon identifier.
Equipment strips the previous weapon overlay before adding the selected policy sections.
Unequipment removes its marked sections and preserves standing policy outside those markers.

The supervisor mounts runtime channels and the chunk graph when it installs the engagement.
For a changed weapon graph, start a new supervised session with the selected equipment.
An active task also checks its loadout hashes before target actions.

## Pack

A Pack contains domain alphabet declarations, validator bindings, workflow templates, detection hints, and policy defaults.
The supported alphabet shapes are categorical, matrix, template-set, and vector.

Select a Pack when creating a capability through Forge.
Review the resulting projected vocabulary and workflow in the draft.
The sealed weapon carries the selected materialized definitions into the runtime.
Pack selection has no separate active runtime slot.

## Trim

A Trim supplies concrete engagement context.
It can include target scope, exclusions, authorization, operating windows, activity posture, cadence, stopping criteria, and operator preferences.
Sew derives this material from the structured engagement form.

The generated files include metadata, `bulwark-section.md`, `absolute-trim.md`, and source context.
Apply operations add marked sections to the effective policy and instruction material.
Release operations remove those sections.

```sh
ara trim list --help
ara trim derive --help
ara trim apply --help
ara trim release --help
```

Keep task-specific targets and time limits in the Trim.
Use standing armor and Temper material for reusable controls.

## Temper

A first-class Temper is a reusable hardening package with its own policy files.
Several installed Tempers can appear in the equipped stack.
Equipment adds the applicable Bulwark block.
ABSOLUTE composition reads doctrine from the active stack.

The current Armory creation page provides curated baseline hardening.

1. Open Temper in Armory.
2. Examine the baseline doctrine and control summary.
3. Install the selected package.
4. Equip the installed Temper.
5. Examine the effective Bulwark rules and instruction envelope.

Reviewed proposal entries can also become a first-class Temper through selected ingestion.
The older armor-scoped proposal path retains separate seal, apply, activate, release, and task projection operations.

Tailor and Needle provide programmatic candidate authoring interfaces.
Candidates have a source citation, rationale, risk, review decision, and destination-specific control object.
The available destinations cover doctrine, Bulwark, canaries, model mediation, approval gates, alerts, and reusable Trim templates.
These authoring interfaces are distinct from the current curated creation page.

## Assemble a task loadout

1. Select the persistent armor.
2. Equip the reviewed weapon.
3. Apply the engagement Trim.
4. Equip the reviewed Temper.
5. Examine the active equipment and policy.
6. Start the task through Armory.

Current Run Task preflight checks the active armor, sealed weapon, active Trim, active-testing permission, and the applicable end time.
It also checks the equipped first-class Temper or the supported materialized armor-scoped Temper.
The task record stores the checked equipment identities and hashes.

Use a new task context when equipment changes are intended to change the run.
This keeps policy updates and task continuity explicit.

Implementation: [set_active_armor](../src/ara/armors.py#L371), [load_armor](../src/ara/armors.py#L436), [equip_policy](../src/ara/weapons.py#L209), [unequip_policy](../src/ara/weapons.py#L252), [_maybe_install_engagement](../src/ara/supervisor.py#L1514), [AlphabetPack](../src/ara/forge/alphabets/pack.py#L131), [build_pack_draft](../src/ara/forge/pack_draft.py#L182), [seal_pack](../src/ara/forge/phases/seal.py#L230), [parse_derive_form](../src/ara/armory/trim_views.py#L573), [derive_trim](../src/ara/trims.py#L806), [apply_trim](../src/ara/trims.py#L643), [handle_temper_create](../src/ara/armory/temper_views.py#L498), [handle_temper_tailor_start](../src/ara/armory/temper_views.py#L549), [ingest](../src/ara/tempers.py#L118), [equip](../src/ara/tempers.py#L247), [call_tool](../src/ara/clients/temper_mcp_server.py#L514), [_validate_tailor_candidate](../src/ara/temper_lifecycle.py#L195), [add_tailor_coverage_summary](../src/ara/temper_lifecycle.py#L594), [validate_control](../src/ara/temper_controls.py#L153), [check_active_trim_for_task](../src/ara/task_preflight.py#L25), [_start_task_session](../src/ara/armory/app.py#L1084), [record_action](../src/ara/task_session.py#L173), [record_commit](../src/ara/task_session.py#L273), [current_loadout_matches](../src/ara/task_session.py#L415).
