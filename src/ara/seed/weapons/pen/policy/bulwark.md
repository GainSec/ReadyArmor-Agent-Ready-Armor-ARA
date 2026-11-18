# Weapon policy is added to the active Bulwark file.
# Equipment markers identify the added section.
# Unequip removes that section and preserves the standing policy.

## global
allow fs   **/.ara/forge/*/mats/**
allow fs   **/.ara/forge/*/manifest.jsonl
allow fs   **/.ara/forge/*/extractions/**
allow fs   **/.ara/forge/*/draft/**
allow fs   **/.ara/forge/*/pack_draft.json
allow fs   **/.ara/forge/*/audit.jsonl
allow fs   **/.ara/sessions/*/events.jsonl
allow fs   **/.ara/sessions/*/transcript.jsonl
allow fs   **/.ara/weapons/pen/**
allow fs   /tmp/**
allow fs   /usr/share/**
allow fs   /usr/lib/**
allow nav  **/.ara/forge/*/**
allow nav  **/.ara/sessions/*/**
allow nav  /tmp/**
allow net  api.openai.com:443
allow net  api.anthropic.com:443
allow net  127.0.0.1:*
allow net  ::1:*
allow url  https://api.openai.com/**
allow url  https://api.anthropic.com/**
allow exec /usr/bin/pdftotext
allow exec /usr/bin/mutool
allow exec /usr/bin/pandoc
allow exec /usr/bin/file
allow exec /usr/bin/jq
allow exec /usr/local/bin/tesseract
allow exec /opt/homebrew/bin/pdftotext
allow exec /opt/homebrew/bin/mutool
allow exec /opt/homebrew/bin/pandoc
allow exec /opt/homebrew/bin/jq
allow exec /opt/homebrew/bin/tesseract
allow oc:read  **/.ara/forge/*/**
allow oc:read  **/.ara/weapons/pen/**
allow oc:write **/.ara/forge/*/extractions/**
allow oc:edit  **/.ara/forge/*/extractions/**
allow oc:glob  **/.ara/forge/*/**
allow oc:grep  **/.ara/forge/*/**
