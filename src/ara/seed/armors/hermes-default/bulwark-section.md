# Armor rules remain active with the equipped weapon.
# Global deny rules have priority over armor rules.

## armor: hermes-default
deny     fs   **/.ssh/**
deny     fs   **/.aws/**
deny     fs   **/.gnupg/**
deny     fs   **/.kube/**
deny     fs   **/.npmrc
deny     fs   /etc/shadow
deny     fs   /etc/sudoers
deny     fs   /etc/sudoers.d/**
deny     fs   **/.hermes/.env
deny     fs   **/.hermes/config.yaml
deny     fs   **/.hermes/SOUL.md
deny     fs   **/.hermes/skills/**
deny     fs   **/.hermes/hooks/**
deny     fs   **/.hermes/memories/**
deny     fs   **/.hermes/cron/**
deny     fs   **/.hermes/pairing/**
deny     exec rm
deny     exec sudo
deny     exec ssh
deny     exec scp
deny     exec curl
deny     exec wget
deny     exec dd
deny     exec chmod
deny     exec chown
deny     ask regex:(?i)(ignore|disregard|forget)\s+(previous|prior|above|earlier)\s+(instruction|prompt|rule)
deny     ask regex:(?i)you\s+are\s+now\s+(a|an)\s+\w*\s*(unrestricted|uncensored|jailbroken)
deny     ask regex:(?i)(skip|bypass)\s+(approval|policy|bulwark|ara|guard)
approval fs   **/Documents/**
allow    net  discord.com:443
allow    net  *.discord.com:443
allow    net  *.discord.gg:443
allow    net  *.discordapp.com:443
allow    net  *.discordapp.net:443
