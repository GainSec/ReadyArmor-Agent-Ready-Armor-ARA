# Armor rules remain active with the equipped weapon.
# Global deny rules have priority over armor rules.

## armor: blacksmith
deny oc:bash      **
deny oc:webfetch  **
deny oc:websearch **
deny oc:task      **
deny oc:notebook  **
deny fs   **/.ssh/**
deny fs   **/.aws/**
deny fs   **/.gnupg/**
deny fs   **/.kube/**
deny fs   **/.npmrc
deny fs   /etc/shadow
deny fs   /etc/sudoers
deny perm /etc/**
deny perm /usr/**
deny perm /boot/**
allow net  api.openai.com:443
allow net  api.anthropic.com:443
allow net  127.0.0.1:*
allow net  ::1:*
deny net   *:*
allow url  https://api.openai.com/**
allow url  https://api.anthropic.com/**
deny env  AWS_*
deny env  *_TOKEN
deny env  *_KEY
deny env  *_SECRET
deny env  GH_TOKEN
deny env  GITHUB_TOKEN
deny env  ANTHROPIC_*
deny env  OPENAI_*
deny ask regex:(?i)(ignore|disregard|forget)\s+(previous|prior|above|earlier)\s+(instruction|prompt|rule)
deny ask regex:(?i)you\s+are\s+now\s+(a|an)\s+\w*\s*(unrestricted|uncensored|jailbroken)
deny ask regex:(?i)(skip|bypass)\s+(approval|policy|bulwark|ara|guard)
deny acp **
