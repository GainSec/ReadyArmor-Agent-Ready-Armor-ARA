# Weapon policy is added to the active Bulwark file.
# Equipment markers identify the added section.
# Unequip removes that section and preserves the standing policy.

## global
allow fs   **/.ara/armors/*/absolutes/**
allow fs   **/.ara/armors/*/temper/**
allow fs   **/.ara/armors/*/trims/**
allow fs   **/.ara/weapons/*/seal.json
allow fs   **/.ara/weapons/*/weapon.json
allow fs   **/.ara/weapons/*/policy/**
allow fs   **/.ara/weapons/needle/**
allow fs   **/.ara/forge/*/mats/**
allow fs   **/.ara/forge/*/manifest.jsonl
allow fs   **/.ara/forge/*/extractions/**
allow fs   **/.ara/forge/*/draft/**
allow fs   **/.ara/sessions/*/events.jsonl
allow fs   **/.ara/sessions/*/transcript.jsonl
allow nav  **/.ara/armors/**
allow nav  **/.ara/weapons/**
allow nav  **/.ara/forge/*/**
allow nav  **/.ara/sessions/*/**
allow net  api.openai.com:443
allow net  api.anthropic.com:443
allow net  127.0.0.1:*
allow net  ::1:*
allow url  https://api.openai.com/**
allow url  https://api.anthropic.com/**
allow oc:read  **/.ara/armors/**
allow oc:read  **/.ara/weapons/**
allow oc:read  **/.ara/forge/*/**
allow oc:read  **/.ara/sessions/*/**
allow oc:glob  **/.ara/armors/**
allow oc:glob  **/.ara/weapons/**
allow oc:glob  **/.ara/forge/*/**
allow oc:grep  **/.ara/armors/**
allow oc:grep  **/.ara/weapons/**
allow oc:grep  **/.ara/forge/*/**
