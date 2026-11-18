# Trim structure and lifecycle

A Trim carries concrete engagement context under its parent armor.
Its context can include targets, excluded assets, authorization, operating windows, activity posture, cadence, and stopping criteria.
Sew derives these values from the structured engagement form.

## Stored material

```text
armors/<armor>/trims/<trim>/
  meta.json
  bulwark-section.md
  absolute-trim.md
  source/
```

Metadata carries the engagement fields.
The policy fragment supplies applicable resource controls.
The instruction fragment supplies the associated model context.
The source directory preserves derivation material where supplied.

## Apply and release

1. Examine the generated Trim metadata and policy.
2. Release a different active Trim before applying the new one.
3. Apply the selected Trim.
4. Examine the effective policy and instruction envelope.
5. Release the Trim when the engagement context ends.

One Trim can be active per armor.
Apply writes marked sections into live policy and the selected armor instruction file.
Reapplication replaces the relevant prior marked section.
Release removes the corresponding section and active marker.

When expiry is available, application bounds matching allow and approval rules with the applicable duration.
Deny rules remain until the marked Trim section is released.
This keeps the boundary in place after time-limited access expires.

## Task equipment

Current Run Task preflight checks Armor, a sealed Weapon, an active Trim, and the applicable Temper state.
It checks active-testing permission and the end time when specified.
A first-class equipped Temper is separate from the Trim.
The current Armory Temper page installs curated hardening.

## Sources and operation

- [Trim derivation](../../src/ara/trims.py#L806).
- [Trim application](../../src/ara/trims.py#L643).
- [Task preflight](../../src/ara/task_preflight.py#L25).
- [Equipment guide](../equipment.md).
- [End-to-end preparation](../end-to-end.md).
