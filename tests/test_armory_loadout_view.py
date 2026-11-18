import json
import re
from pathlib import Path

from ara import armors as armors_mod
from ara import temper as temper_mod
from ara import trims as trims_mod
from ara import weapons as weapons_mod
from ara.armory import trim_views
from ara.armory import views
from ara.armory.forge_v1_views import render_weapon_detail, render_weapons


def _seal_weapon(state: Path, weapon_id: str) -> None:
    wdir = weapons_mod.weapon_dir(state, weapon_id)
    wdir.mkdir(parents=True)
    (wdir / "seal.json").write_text(
        json.dumps(
            {
                "weapon_hash": "demo-hash",
                "weapon_target_id": weapon_id,
                "forge_id": "demo-forge",
                "operator_id": "operator",
                "sealed_at": 0,
                "warnings": [],
            }
        )
    )


def test_status_page_renders_real_equipped_loadout(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    armors_mod.create_armor(
        state,
        "blacksmith",
        description="Hermes default armor",
        sigil="hammer",
    )
    armors_mod.set_channels(state, "blacksmith", ["discord", "telegram"])
    armors_mod.set_active_armor(state, "blacksmith")

    _seal_weapon(state, "blacksmith-hammer")
    weapons_mod.set_equipped_weapon(state, "blacksmith-hammer")

    trims_mod.new_trim(state, "blacksmith", "acme-engagement", target="Acme")
    trims_mod.apply_trim(state, "blacksmith", "acme-engagement")
    proposal = temper_mod.create_proposal(
        state, "blacksmith", source_trims=["acme-engagement"],
    )
    sealed = temper_mod.seal_selected_proposal(
        state, "blacksmith", proposal.proposal_id, selected_indexes=[0],
    )
    temper_mod.activate_temper(state, "blacksmith", sealed.proposal_id)

    html = views.render_status(state, "tok")

    assert 'class="ara-loadout-stage"' in html
    assert html.count('class="bc-card') >= 4
    assert html.count('class="ara-pixel-art ara-pixel-art-') >= 4
    assert 'class="ara-pixel-loadout-agent"' in html
    assert 'data-loadout-style="paper-doll"' in html
    assert 'data-active-armor="blacksmith"' in html
    assert 'data-active-weapon="blacksmith-hammer"' in html
    assert 'data-trim-seed="acme-engagement"' in html
    assert f'data-temper-seed="{sealed.proposal_id}"' in html
    assert 'data-loadout-kind="armor"' in html
    assert 'data-loadout-kind="weapon"' in html
    assert 'data-loadout-kind="trim"' in html
    assert 'data-loadout-kind="temper"' in html
    assert 'class="px-agent-armor-sigil"' in html
    assert 'class="px-agent-weapon-head"' in html
    assert 'class="px-agent-trim-ribbons"' in html
    assert 'class="px-agent-temper-runes"' in html
    assert 'class="px-agent-trim-overlay"' in html
    assert 'class="px-agent-temper-overlay"' in html
    assert 'data-kit-kind="armor"' in html
    assert 'data-kit-kind="weapon"' in html
    assert 'data-kit-kind="trim"' in html
    assert 'data-kit-kind="temper"' in html
    assert 'class="ara-kit-card ara-kit-card-armor"' not in html
    assert 'class="ara-kit-card ara-kit-card-weapon"' not in html
    assert 'class="ara-kit-card ara-kit-card-trim"' not in html
    assert "blacksmith-hammer" in html
    assert "acme-engagement" in html
    assert sealed.proposal_id in html
    assert "Gateways:" in html
    assert "Discord" in html
    assert "Telegram" in html
    assert 'class="ara-loadout-hand"' in html
    assert 'class="status-vitals-grid"' in html
    assert 'class="status-vital-card status-vital-runtime"' in html
    assert 'class="status-vital-card status-vital-ara"' in html
    assert 'class="status-vital-card status-vital-session"' in html
    assert "Worker runtime" in html
    assert "ARA install" in html
    assert "Live session" in html
    assert 'class="ara-dl status-posture-dl"' not in html


def test_pixel_art_is_deterministic_and_varies_by_kind() -> None:
    armor_one = views._render_pixel_art("armor", "hermes-default", "Hermes")
    armor_two = views._render_pixel_art("armor", "hermes-default", "Hermes")
    weapon = views._render_pixel_art("weapon", "hermes-default", "Hermes")

    assert armor_one == armor_two
    assert armor_one != weapon
    assert 'class="ara-pixel-art ara-pixel-art-armor"' in armor_one
    assert 'viewBox="0 0 32 32"' in armor_one
    assert 'data-pixel-quality="32"' in armor_one
    assert 'data-pixel-style="readable-silhouette"' in armor_one
    assert 'data-pixel-variant="' in armor_one
    assert 'class="px-layer px-outline"' in armor_one
    assert '<rect x="0" y="0" width="32" height="32"' not in armor_one
    assert armor_one.count("<rect") >= 24
    assert weapon.count("<rect") >= 18
    assert 'viewBox="0 0 16 16"' not in armor_one


def test_pixel_gallery_renders_seeded_variants() -> None:
    html = views.render_pixel_gallery("tok")

    assert 'class="pixel-gallery"' in html
    assert 'data-pixel-gallery-kind="armor"' in html
    assert 'data-pixel-gallery-kind="weapon"' in html
    assert 'data-pixel-gallery-kind="trim"' in html
    assert html.count('class="ara-pixel-art ara-pixel-art-') >= 24
    assert "/armors?t=tok" in html
    assert "/weapons?t=tok" in html
    assert "/trims?t=tok" in html


def test_pixel_art_exposes_multiple_readable_variants_per_kind() -> None:
    seeds = [f"variant-seed-{idx}" for idx in range(80)]

    def variants(kind: str) -> set[str]:
        out: set[str] = set()
        for seed in seeds:
            html = views._render_pixel_art(kind, seed, seed)
            match = re.search(r'data-pixel-variant="([^"]+)"', html)
            assert match is not None
            out.add(match.group(1))
        return out

    assert len(variants("armor")) >= 4
    assert len(variants("weapon")) >= 5
    assert len(variants("trim")) >= 4


def test_status_runtime_prefers_hermes_when_available(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / ".ara"
    armors_mod.create_armor(state, "hermes-default", description="Hermes default")
    armors_mod.set_active_armor(state, "hermes-default")

    monkeypatch.setattr(
        views,
        "_runtime_infos",
        lambda: [
            {
                "name": "openclaw",
                "binary": "/usr/local/bin/openclaw",
                "version": "OpenClaw 2026.4.15",
                "state_dir": "~/.openclaw",
                "state_dir_size": 10,
                "config_path": "~/.openclaw/openclaw.json",
            },
            {
                "name": "hermes",
                "binary": "~/.local/bin/hermes",
                "version": "hermes-default",
                "state_dir": "~/.hermes",
                "state_dir_size": 20,
                "config_path": "~/.hermes/config.yaml",
            },
        ],
    )

    html = views.render_status(state, "tok")

    assert "<h3>hermes</h3>" in html
    assert "<h3>openclaw</h3>" not in html


def test_armors_page_focuses_on_equipped_armor_detail(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    armor = armors_mod.create_armor(
        state,
        "blacksmith",
        description="Hermes default armor",
        sigil="hammer",
    )
    armor.walls_path.write_text("## deny_path\n~/.ssh/**\n")
    armor.absolute_path.write_text("# Role\n\nGuard Hermes.\n")
    armors_mod.set_channels(state, "blacksmith", ["discord"])
    armors_mod.set_active_armor(state, "blacksmith")

    _seal_weapon(state, "blacksmith-hammer")
    weapons_mod.set_equipped_weapon(state, "blacksmith-hammer")

    trims_mod.new_trim(state, "blacksmith", "acme-engagement", target="Acme")
    trims_mod.apply_trim(state, "blacksmith", "acme-engagement")

    html = views.render_armors(state, "tok")

    assert 'class="ara-armor-focus"' in html
    assert 'class="ara-armor-info-grid"' in html
    assert "Equipped armor" in html
    assert "Armor set detail" not in html
    assert '<summary><span class="ara-eyebrow">Detail</span>' in html
    assert '<details class="ara-armor-detail-inline">' in html
    assert '<details open><summary>Identity</summary>' not in html
    assert "Rule breakdown" in html
    assert "ABSOLUTE.md" in html
    assert "Channel scope" in html
    assert "blacksmith-hammer" not in html
    assert 'class="ara-kit-card ara-kit-card-weapon"' not in html
    assert 'class="ara-kit-card ara-kit-card-trim"' not in html


def test_nav_groups_sew_under_create() -> None:
    groups = {group: items for group, items in views._NAV_GROUPS}
    labels = [(group, [label for _, label, _ in items]) for group, items in views._NAV_GROUPS]

    assert "Create" in [group for group, _ in labels]
    assert "Keep" not in [group for group, _ in labels]
    assert "Barracks" in [group for group, _ in labels]
    assert "Sew" not in [group for group, _ in labels]
    assert "Blacksmith" not in [group for group, _ in labels]
    assert "Forge-v1" not in [group for group, _ in labels]
    assert "Policy" not in [group for group, _ in labels]

    create = {label: href for _, label, href in groups["Create"]}
    assert create["Forge"] == "/forge"
    assert create["Sew"] == "/trims/derive"
    assert create["KEEP"] == "/mats"

    barracks = {label: href for _, label, href in groups["Barracks"]}
    assert barracks["Weapons"] == "/weapons"
    assert barracks["Armor"] == "/armors"
    assert barracks["Trims"] == "/trims"
    assert barracks["MATS"] == "/mats/inventory"
    assert "Armor Sets" not in barracks
    assert "Mats" not in barracks


def test_sew_create_pages_highlight_sew_not_trims(tmp_path: Path) -> None:
    derive_html = trim_views.render_trim_derive(tmp_path / ".ara", "tok")
    new_html = trim_views.render_trim_new(tmp_path / ".ara", "tok")
    index_html = trim_views.render_trims_index(tmp_path / ".ara", "tok")

    assert '<a href="/trims/derive?t=tok" class="active">Sew</a>' in derive_html
    assert '<a href="/trims?t=tok">Trims</a>' in derive_html
    assert '<a href="/trims/derive?t=tok" class="active">Sew</a>' in new_html
    assert '<a href="/trims?t=tok">Trims</a>' in new_html

    assert '<a href="/trims/derive?t=tok">Sew</a>' in index_html
    assert '<a href="/trims?t=tok" class="active">Trims</a>' in index_html


def test_sew_page_uses_create_language_and_one_submit(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    armors_mod.create_armor(state, "hermes-default")

    html = trim_views.render_trim_derive(state, "tok")

    assert 'id="sew-trim-form"' in html
    assert 'form="sew-trim-form"' not in html
    assert html.count(">Create trim</button>") == 1
    assert "Derive Trim" not in html
    assert "<title>ARA · Create Trim</title>" in html
    assert '<h2 class="ara-h">Create Trim from Task</h2>' in html


def test_sew_page_sections_are_collapsed_disclosures(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    armors_mod.create_armor(state, "hermes-default")

    html = trim_views.render_trim_derive(state, "tok")

    assert "<fieldset>" not in html
    assert html.count('class="sew-form-section"') >= 10
    assert 'class="sew-form-section" open' not in html
    for label in [
        "Task",
        "Target &amp; Scope",
        "Environment",
        "Posture (knowledge level)",
        "Activity",
        "Depth tier",
        "Test Window",
        "ROE Attestation",
        "Delivery Channels",
        "Rate limits",
        "Stop conditions",
        "Operator Preferences",
        "Misc",
    ]:
        assert f"<summary>{label}" in html


def test_item_creation_pages_include_generated_pixel_previews(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    armors_mod.create_armor(state, "hermes-default")

    armor_html = views.render_armor_new("tok")
    trim_derive_html = trim_views.render_trim_derive(state, "tok")
    trim_new_html = trim_views.render_trim_new(state, "tok")

    assert "/static/armor-card.css" in armor_html
    assert 'class="ara-creation-preview ara-creation-preview-armor"' in armor_html
    assert 'class="ara-pixel-art ara-pixel-art-armor"' in armor_html

    assert "/static/armor-card.css" in trim_derive_html
    assert 'class="ara-creation-preview ara-creation-preview-trim"' in trim_derive_html
    assert 'class="ara-pixel-art ara-pixel-art-trim"' in trim_derive_html

    assert "/static/armor-card.css" in trim_new_html
    assert 'class="ara-creation-preview ara-creation-preview-trim"' in trim_new_html
    assert 'class="ara-pixel-art ara-pixel-art-trim"' in trim_new_html


def test_weapons_page_uses_armor_baseline_layout(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    _seal_weapon(state, "blacksmith-hammer")
    _seal_weapon(state, "scout-lens")
    weapons_mod.set_equipped_weapon(state, "blacksmith-hammer")

    html = render_weapons(state, "tok")

    assert 'class="ara-armor-focus ara-weapon-focus"' in html
    assert 'class="ara-armor-info-grid"' in html
    assert '<summary><span class="ara-eyebrow">Detail</span>' in html
    assert '<details class="ara-armor-detail-inline">' in html
    assert "Other weapons" in html
    assert "scout-lens" in html


def test_weapons_page_does_not_label_standby_weapon_as_equipped(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    _seal_weapon(state, "blacksmith-hammer")

    html = render_weapons(state, "tok")

    assert "No weapon equipped" in html
    assert "Available weapon" in html
    assert "Equipped weapon" not in html
    assert ">Equip</button>" in html
    assert ">Unequip</button>" not in html
    assert "standby" in html


def test_trims_page_uses_armor_baseline_layout(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    armors_mod.create_armor(state, "blacksmith", description="Hermes default")
    armors_mod.set_active_armor(state, "blacksmith")
    trims_mod.new_trim(state, "blacksmith", "acme-engagement", target="Acme")
    trims_mod.new_trim(state, "blacksmith", "beta-engagement", target="Beta")
    trims_mod.apply_trim(state, "blacksmith", "acme-engagement")

    html = trim_views.render_trims_index(state, "tok")

    assert 'class="ara-armor-focus ara-trim-focus"' in html
    assert 'class="ara-armor-info-grid"' in html
    assert '<summary><span class="ara-eyebrow">Detail</span>' in html
    assert '<details class="ara-armor-detail-inline">' in html
    assert "Other trims" in html
    assert "beta-engagement" in html


def test_item_detail_pages_include_generated_pixel_art(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    armor = armors_mod.create_armor(
        state,
        "blacksmith",
        description="Hermes default armor",
        sigil="hammer",
    )
    armors_mod.set_active_armor(state, armor.name)
    _seal_weapon(state, "blacksmith-hammer")
    weapons_mod.set_equipped_weapon(state, "blacksmith-hammer")
    trims_mod.new_trim(state, armor.name, "acme-engagement", target="Acme")
    trims_mod.apply_trim(state, armor.name, "acme-engagement")

    armor_html = views.render_armor_detail(state, armor.name, "tok")
    weapon_html = render_weapon_detail(state, "blacksmith-hammer", "tok")
    trim_html = trim_views.render_trim_detail(
        state,
        armor.name,
        "acme-engagement",
        "tok",
    )

    assert armor_html is not None
    assert "/static/armor-card.css" in armor_html
    assert 'class="ara-pixel-art ara-pixel-art-armor"' in armor_html
    assert 'data-pixel-style="readable-silhouette"' in armor_html

    assert "/static/armor-card.css" in weapon_html
    assert 'class="ara-pixel-art ara-pixel-art-weapon"' in weapon_html
    assert 'data-pixel-style="readable-silhouette"' in weapon_html
    assert "blacksmith-hammer" in weapon_html

    assert "/static/armor-card.css" in trim_html
    assert 'class="ara-pixel-art ara-pixel-art-trim"' in trim_html
    assert 'data-pixel-style="readable-silhouette"' in trim_html
    assert "acme-engagement" in trim_html
