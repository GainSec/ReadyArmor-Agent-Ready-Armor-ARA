"""End-to-end lifecycle tests for blacksmith-hammer auto-equip on
``ara forge new`` and auto-unequip on ``ara forge abort`` / seal.

Per docs/specs/blacksmith-hammer-v0.md §7 + §10. Covers:

- First-run install of seeds is idempotent.
- ``forge new`` on a fresh state copies seeds, equips both armor + weapon,
  merges bulwark.md cleanly (parse_errors empty).
- ``forge abort`` restores prior armor + weapon and strips the weapon
  overlay; armor scope (the seed install) persists.
- Operator-curated content above and below the markers survives a full
  equip → unequip cycle byte-for-byte.
- Seed-stability: re-running ``forge new`` does not duplicate seed dirs
  or scope sections.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ara import armors as armors_mod
from ara import bulwark as bulwark_mod
from ara import weapons as weapons_mod
from ara.seed import ensure_seed


def _run_forge(state: Path, *args: str) -> dict:
    env = {"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"}
    res = subprocess.run(
        [sys.executable, "-m", "ara.cli", "forge",
         "--state-root", str(state), *args],
        capture_output=True, text=True, env=env,
    )
    if res.returncode != 0:
        raise AssertionError(
            f"ara forge {' '.join(args)} failed (rc={res.returncode})\n"
            f"stdout: {res.stdout}\nstderr: {res.stderr}"
        )
    return json.loads(res.stdout) if res.stdout.strip() else {}


def _seed_prior_armor(state: Path, name: str) -> None:
    (state / "armors" / name).mkdir(parents=True, exist_ok=True)
    (state / "armors" / name / "WALLS.md").write_text(f"## deny_path\n# {name}\n")
    (state / "active-armor").write_text(name)


def _seed_prior_weapon(state: Path, weapon_id: str, body: str) -> None:
    pdir = state / "weapons" / weapon_id / "policy"
    pdir.mkdir(parents=True, exist_ok=True)
    (state / "weapons" / weapon_id / "seal.json").write_text("{}")
    (pdir / "bulwark.md").write_text(body)
    (state / "equipped_weapon").write_text(weapon_id)


# ---------- ensure_seed primitives ----------

def test_ensure_seed_first_run_is_idempotent(tmp_path: Path) -> None:
    rep1 = ensure_seed(tmp_path)
    assert "blacksmith" in rep1.copied_armors
    assert "blacksmith-hammer" in rep1.copied_weapons
    assert "blacksmith" in rep1.bulwark_sections_applied

    text1 = (tmp_path / "bulwark.md").read_text()
    rep2 = ensure_seed(tmp_path)
    text2 = (tmp_path / "bulwark.md").read_text()

    assert rep2.copied_armors == []
    assert rep2.copied_weapons == []
    assert rep2.bulwark_sections_unchanged == ["blacksmith"]
    assert text1 == text2


def test_ensure_seed_preserves_operator_content(tmp_path: Path) -> None:
    operator = (
        "## global\n"
        "deny fs **/.aws/**\n"
        "deny net analytics.example.com\n"
    )
    (tmp_path / "bulwark.md").write_text(operator)
    ensure_seed(tmp_path)

    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()
    matches = [r for r in bw.rules if "/.aws/" in (r.match.primary or "")]
    assert any(r.scope.raw == "global" for r in matches)


def test_ensure_seed_armor_scope_section_parses_cleanly(tmp_path: Path) -> None:
    ensure_seed(tmp_path)
    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()
    armor_scoped = [r for r in bw.rules if "blacksmith" in (r.scope.raw or "")]
    assert len(armor_scoped) >= 13   # spec §4: identity-deny rule count
    # Sample expected layers
    layers = sorted(set(r.layer for r in armor_scoped))
    for must in ("oc:bash", "oc:webfetch", "fs", "perm", "env", "ask", "acp"):
        assert must in layers, f"missing layer {must!r} in armor scope"


def test_blacksmith_seed_defaults_to_forge_only_egress(tmp_path: Path) -> None:
    ensure_seed(tmp_path)
    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()
    armor_scoped = [
        r for r in bw.rules
        if r.scope.raw == "armor: blacksmith" and r.layer in {"net", "url"}
    ]
    allow_net = {
        r.match.primary for r in armor_scoped
        if r.action == "allow" and r.layer == "net"
    }
    deny_net = {
        r.match.primary for r in armor_scoped
        if r.action == "deny" and r.layer == "net"
    }
    allow_url = {
        r.match.primary for r in armor_scoped
        if r.action == "allow" and r.layer == "url"
    }

    assert "api.openai.com:443" in allow_net
    assert "api.anthropic.com:443" in allow_net
    assert "127.0.0.1:*" in allow_net
    assert "*:*" in deny_net
    assert "https://api.openai.com/**" in allow_url
    assert "https://api.anthropic.com/**" in allow_url


def test_tailor_needle_seeds_materialize_temper_identity(tmp_path: Path) -> None:
    rep = ensure_seed(
        tmp_path,
        armors=("tailor-robes",),
        weapons=("needle",),
    )

    assert rep.copied_armors == ["tailor-robes"]
    assert rep.copied_weapons == ["needle"]
    assert rep.bulwark_sections_applied == ["tailor-robes"]

    armor_root = tmp_path / "armors" / "tailor-robes"
    weapon_root = tmp_path / "weapons" / "needle"
    assert (armor_root / "absolutes" / "active").read_text().strip() == "default"
    assert "sole purpose is to derive Temper candidates" in (
        armor_root / "absolutes" / "default.md"
    ).read_text()

    seal = json.loads((weapon_root / "seal.json").read_text())
    assert seal["id"] == "needle"
    assert seal["weapon_target_id"] == "needle"
    assert seal["temper_channels"] == [
        "temper.absolute_candidate",
        "temper.bulwark_candidate",
        "temper.canary_candidate",
        "temper.rhema_candidate",
        "temper.approval_gate_candidate",
        "temper.alert_hook_candidate",
        "temper.trim_template_candidate",
        "temper.coverage_summary",
    ]

    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()
    armor_scoped = [r for r in bw.rules if r.scope.raw == "armor: tailor-robes"]
    assert any(r.layer == "oc:websearch" and r.action == "deny" for r in armor_scoped)
    assert any(r.layer == "acp" and r.action == "deny" for r in armor_scoped)
    assert any(r.layer == "net" and r.action == "deny" and r.match.primary == "*:*"
               for r in armor_scoped)

    overlay = weapons_mod.equip_policy(tmp_path, "needle")
    assert "bulwark.md" in overlay["overlaid"]
    bw_with_weapon = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw_with_weapon.parse_errors == ()
    assert "# ARA-WEAPON-OVERLAY-BEGIN needle" in (
        tmp_path / "bulwark.md"
    ).read_text()


def test_tailor_needle_seed_files_are_parseable_and_temper_only() -> None:
    root = ensure_seed.__globals__["_SEED_ROOT"]
    armor_root = root / "armors" / "tailor-robes"
    weapon_root = root / "weapons" / "needle"
    profile_root = root / "hermes-profiles" / "temper"

    meta = json.loads((armor_root / "meta.json").read_text())
    assert "temper" in meta["tags"]
    assert meta["sigil"] == "needle"
    assert meta["absolute_enforce"] is True

    default_abs = (armor_root / "absolutes" / "default.md").read_text()
    assert "TOD" not in default_abs
    assert "rate limit" not in default_abs.lower()
    assert "stop condition" not in default_abs.lower()
    assert "MITM" not in default_abs
    assert "gateway settings" not in default_abs

    bw = bulwark_mod.parse_file(armor_root / "bulwark-section.md")
    assert bw.parse_errors == ()
    assert any(r.layer == "oc:webfetch" and r.action == "deny" for r in bw.rules)

    catalog = json.loads((weapon_root / "channel_catalog.json").read_text())
    channels = [ch["name"] for ch in catalog["channels"]]
    assert "temper.canary_candidate" in channels
    assert "temper.rhema_candidate" in channels
    assert "temper.alert_hook_candidate" in channels
    assert "temper.coverage_summary" in channels
    assert all(not ch.startswith("extract.") for ch in channels)

    soul = (profile_root / "SOUL.md").read_text()
    assert "Tailor Robes" in soul
    assert "Needle" in soul
    assert "Do not generate weapons" in soul
    assert "coverage matrix" in soul.lower()
    assert "source-by-source" in soul.lower()
    assert "destination-by-destination" in soul.lower()
    assert "at least 12 candidates per source element" in soul
    assert "12-20 useful candidates per source" in soul
    assert "temper.coverage_summary" in soul
    assert "why no candidate" in soul.lower()
    for destination in (
        "canary",
        "rhema",
        "absolute",
        "approval_gate",
        "alert_hook",
        "bulwark",
        "trim_template",
    ):
        assert destination in soul

    config = (profile_root / "config.yaml").read_text()
    assert "ara-temper:" in config
    assert 'args: ["-m", "ara.cli", "temper-mcp"]' in config
    assert 'ARA_STATE_ROOT: "${ARA_STATE_ROOT}"' in config
    assert "ARA_SOCKET" not in config
    assert "ara-forge" not in config
    assert "forge-mcp" not in config
    assert "cli: []" in config
    assert "max_turns: 48" in config


# ---------- ara forge new auto-equip ----------

def test_forge_new_auto_equips_blacksmith_on_clean_state(tmp_path: Path) -> None:
    out = _run_forge(tmp_path, "new", "test-target")

    assert out["prior_armor"] is None
    assert out["prior_weapon"] is None
    assert out["equipped"]["armor"] == "blacksmith"
    assert out["equipped"]["weapon"] == "blacksmith-hammer"
    assert "blacksmith" in out["equipped"]["seeded_armors"]
    assert "blacksmith-hammer" in out["equipped"]["seeded_weapons"]

    assert (tmp_path / "active-armor").read_text().strip() == "blacksmith"
    assert (tmp_path / "equipped_weapon").read_text().strip() == "blacksmith-hammer"

    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()


def test_forge_new_captures_prior_armor_and_weapon(tmp_path: Path) -> None:
    _seed_prior_armor(tmp_path, "orion")
    _seed_prior_weapon(
        tmp_path, "wprior",
        body="## global\nallow url https://api.example/*\n",
    )

    out = _run_forge(tmp_path, "new", "test-target")
    assert out["prior_armor"] == "orion"
    assert out["prior_weapon"] == "wprior"
    assert (tmp_path / "active-armor").read_text().strip() == "blacksmith"
    assert (tmp_path / "equipped_weapon").read_text().strip() == "blacksmith-hammer"


def test_forge_new_against_already_blacksmith_state_doesnt_loop(tmp_path: Path) -> None:
    """If operator was already running blacksmith (e.g., from a crashed
    prior session), forge new captures None as the prior so unequip
    later doesn't restore blacksmith forever."""
    armors_mod.set_active_armor(tmp_path, "blacksmith")
    weapons_mod.set_equipped_weapon(tmp_path, "blacksmith-hammer")

    out = _run_forge(tmp_path, "new", "test-target")
    assert out["prior_armor"] is None
    assert out["prior_weapon"] is None


# ---------- ara forge abort restores prior ----------

def test_forge_abort_restores_prior_armor_and_weapon(tmp_path: Path) -> None:
    _seed_prior_armor(tmp_path, "orion")
    _seed_prior_weapon(
        tmp_path, "wprior",
        body="## global\nallow url https://api.example/*\n",
    )

    new_out = _run_forge(tmp_path, "new", "test-target")
    fid = new_out["forge_id"]

    abort_out = _run_forge(tmp_path, "abort", fid, "--reason", "test")
    assert abort_out["unequipped"]["restored_armor"] == "orion"
    assert abort_out["unequipped"]["restored_weapon"] == "wprior"

    assert (tmp_path / "active-armor").read_text().strip() == "orion"
    assert (tmp_path / "equipped_weapon").read_text().strip() == "wprior"


def test_forge_abort_strips_weapon_overlay_re_equips_prior(tmp_path: Path) -> None:
    _seed_prior_weapon(
        tmp_path, "wprior",
        body="## global\nallow url https://api.example/*\n",
    )
    new_out = _run_forge(tmp_path, "new", "test-target")
    _run_forge(tmp_path, "abort", new_out["forge_id"], "--reason", "test")

    text = (tmp_path / "bulwark.md").read_text()
    assert "BEGIN blacksmith-hammer" not in text
    assert "BEGIN wprior" in text
    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()
    # Prior weapon's rule restored
    assert any(
        "api.example" in (r.match.primary or "") for r in bw.rules
    )


def test_forge_abort_clean_state_leaves_no_equipped(tmp_path: Path) -> None:
    """No prior weapon/armor → after abort, neither is equipped."""
    new_out = _run_forge(tmp_path, "new", "test-target")
    _run_forge(tmp_path, "abort", new_out["forge_id"], "--reason", "test")

    # active-armor file may or may not exist; both mean "nothing equipped"
    active = armors_mod.active_armor_name(tmp_path)
    equipped = weapons_mod.equipped_weapon_id(tmp_path)
    assert active is None
    assert equipped is None


# ---------- byte-stability across full cycle ----------

def test_full_cycle_preserves_operator_content_byte_for_byte(tmp_path: Path) -> None:
    operator = (
        "# Operator-managed bulwark.md — DO NOT EDIT BELOW THE MARKERS\n"
        "\n"
        "## global\n"
        "deny fs **/.aws/**\n"
        "deny net analytics.example.com\n"
        "approval url https://*.payments.internal/*\n"
        "\n"
        "## global -armor: forge\n"
        "# A custom override that should not be touched by forge\n"
        "allow exec /opt/myextractor/bin/parse\n"
    )
    (tmp_path / "bulwark.md").write_text(operator)

    new_out = _run_forge(tmp_path, "new", "test-target")
    _run_forge(tmp_path, "abort", new_out["forge_id"], "--reason", "test")

    after = (tmp_path / "bulwark.md").read_text()
    # Operator's prefix preserved
    assert after.startswith(operator.split("\n## global -armor")[0])
    # Operator's tail preserved
    assert "/opt/myextractor/bin/parse" in after
    assert "DO NOT EDIT BELOW" in after
    # No weapon overlay residue
    assert "BEGIN blacksmith-hammer" not in after
    # Armor scope (seed install) is permanent
    assert "## armor: blacksmith" in after
    bw = bulwark_mod.parse_file(tmp_path / "bulwark.md")
    assert bw.parse_errors == ()


# ---------- re-running forge new is idempotent on seeds ----------

def test_repeat_forge_new_does_not_duplicate_armor_scope(tmp_path: Path) -> None:
    _run_forge(tmp_path, "new", "target-a")
    _run_forge(tmp_path, "abort",
               next(p.name for p in (tmp_path / "forge").iterdir()),
               "--reason", "first")

    text_after_first = (tmp_path / "bulwark.md").read_text()
    begin_count_first = text_after_first.count(
        "# ARA-ARMOR-SCOPE-BEGIN blacksmith"
    )
    assert begin_count_first == 1

    _run_forge(tmp_path, "new", "target-b")
    text_after_second = (tmp_path / "bulwark.md").read_text()
    begin_count_second = text_after_second.count(
        "# ARA-ARMOR-SCOPE-BEGIN blacksmith"
    )
    assert begin_count_second == 1, (
        f"armor scope BEGIN marker count drifted: "
        f"{begin_count_first} → {begin_count_second}"
    )


# ---------- supplemental sanity: marker-style swap covers prose ----------

def test_armor_scope_strip_ignores_marker_mention_in_prose() -> None:
    """The strip logic must require markers be at line-start; prose
    mentioning the marker token in a comment/docstring shouldn't
    trigger a false strip."""
    from ara.seed import _strip_armor_scope
    text = (
        "## global\n"
        "deny fs ~\n"
        "\n"
        "# Note: see `# ARA-ARMOR-SCOPE-BEGIN blacksmith` "
        "for documentation\n"
        "\n"
        "# ARA-ARMOR-SCOPE-BEGIN blacksmith\n"
        "deny oc:bash *\n"
        "# ARA-ARMOR-SCOPE-END\n"
    )
    out = _strip_armor_scope(text, "blacksmith")
    # Real block stripped
    assert "deny oc:bash" not in out
    # Prose mention preserved
    assert "see `# ARA-ARMOR-SCOPE-BEGIN blacksmith`" in out
    # Operator content preserved
    assert "## global" in out
    assert "deny fs ~" in out


@pytest.fixture(autouse=True)
def _registry_isolation():
    """The full forge.cli import chain may touch the channels registry.
    Reset before each test so registry leaks across tests don't surface
    here."""
    from ara.runtime.channels import registry
    registry.unregister_for_tests()
    yield
    registry.unregister_for_tests()
