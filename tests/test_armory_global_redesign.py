from pathlib import Path

from ara import engagement_mats as em
import ara.armory
from ara.armory import mats_views
from ara.armory import tool_views
from ara.armory import views
from ara.armory.forge_v1_views import render_engagement


def test_shared_redesign_css_covers_legacy_surfaces() -> None:
    css = (
        Path(ara.armory.__file__).parent
        / "static"
        / "armory.css"
    ).read_text()

    assert ".card {" in css
    assert ".ara-badge" in css
    assert ".ara-pill" in css
    assert ".ara-stat-row" in css
    assert ".ara-tab-bar" in css
    assert ".ara-toolbar" in css
    assert ".ara-loadout-hand" in css
    assert ".status-vitals-grid" in css
    assert ".courtyard-hero" in css
    assert ".courtyard-session-grid" in css
    assert ".courtyard-graph-stage" in css
    assert ".bulwark-hero" in css
    assert ".bulwark-doctrine-grid" in css
    assert ".keep-hero" in css
    assert ".keep-class-grid" in css
    assert ".keep-mat-row" in css
    assert "::file-selector-button" in css
    assert ".keep-file-meta" in css


def test_courtyard_uses_shared_redesigned_tabs(tmp_path: Path) -> None:
    html = views.render_courtyard(tmp_path, "tok")

    assert 'class="ara-tab-bar"' in html
    assert 'style="padding:6px 14px' not in html
    assert "Courtyard" in html
    assert 'class="courtyard-hero"' in html
    assert 'class="courtyard-signal-grid"' in html
    assert 'class="courtyard-tab-panel courtyard-sessions-panel"' in html


def test_courtyard_redesign_covers_live_approvals_and_graph(tmp_path: Path) -> None:
    live = views.render_courtyard(tmp_path, "tok", tab="live")
    approvals = views.render_courtyard(tmp_path, "tok", tab="approvals")
    graph = views.render_courtyard(tmp_path, "tok", tab="graph")

    assert 'class="courtyard-live-shell"' in live
    assert 'class="courtyard-stream-rail"' in live
    assert 'class="courtyard-approval-shell"' in approvals
    assert 'class="courtyard-resolution-grid"' in approvals
    assert 'class="courtyard-graph-shell"' in graph
    assert 'class="courtyard-graph-stage"' in graph


def test_bulwark_is_digestible_barracks_surface(tmp_path: Path) -> None:
    (tmp_path / "bulwark.md").write_text(
        "## global\n"
        "deny fs /private/**\n"
        "approval exec /usr/bin/curl\n"
        "allow net api.example.com\n"
    )

    html = views.render_bulwark(tmp_path, "tok")

    assert '<div class="ara-eyebrow">Barracks</div>' in html
    assert '<div class="ara-eyebrow">Policy</div>' not in html
    assert 'class="bulwark-hero"' in html
    assert 'class="bulwark-summary-grid"' in html
    assert 'class="bulwark-doctrine-grid"' in html
    assert 'class="bulwark-rule-card bulwark-rule-deny"' in html
    assert 'class="bulwark-rule-card bulwark-rule-approval"' in html
    assert 'class="bulwark-builder-shell"' in html


def test_bulwark_scope_builder_hides_inactive_scope_detail_rows(tmp_path: Path) -> None:
    (tmp_path / "bulwark.md").write_text("## global\ndeny fs /private/**\n")

    html = views.render_bulwark(tmp_path, "tok")

    assert 'id="bw-armor-label"' in html
    assert (
        'id="bw-armor-label">Armor</label>\n'
        '<select name="scope_armor" id="bw-scope-armor"'
    ) in html
    assert (
        'id="bw-custom-label">Custom scope</label>\n'
        '<input name="scope_custom" id="bw-scope-custom"'
    ) in html
    assert "const armorLbl = document.getElementById('bw-armor-label');" in html
    assert "armorLbl.style.display = (k === 'armor') ? '' : 'none';" in html
    assert "customLbl.style.display = (k === 'custom') ? '' : 'none';" in html


def test_engagement_empty_state_uses_global_card_language(tmp_path: Path) -> None:
    html = render_engagement(tmp_path, "tok")

    assert 'class="card ara-page-card"' in html
    assert '<div class="ara-eyebrow">Live</div>' in html
    assert "No weapon equipped" in html
    assert "Backup Hermes chat" not in html


def test_settings_contains_collapsed_debug_backup_chat(tmp_path: Path) -> None:
    html = views.render_settings(tmp_path, "tok", viewer_token="view")

    assert '<details class="settings-debug-card">' in html
    assert "<summary>" in html
    assert "Debug" in html
    assert "Backup Hermes chat" in html
    assert "/api/ws" in html
    assert "prompt.submit" in html
    assert 'open class="settings-debug-card"' not in html


def test_tools_page_uses_shared_shell_classes(tmp_path: Path) -> None:
    html = tool_views.render_tools_index(tmp_path, "tok")

    assert "<section>" in html
    assert "Tools" in html
    assert "ara-pill" in html or "ara-empty" in html


def test_mats_index_is_keep_management_surface(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    em.add_mat_bytes(
        state,
        "acme-engagement",
        "architecture",
        "overview.md",
        b"system map",
        uploaded_by="operator",
        tags=["seed"],
    )

    html = mats_views.render_mats_index(state, "tok")

    assert '<div class="ara-eyebrow">Keep</div>' in html
    assert "<title>ARA · KEEP</title>" in html
    assert '<a href="/mats?t=tok" class="active">KEEP</a>' in html
    assert '<a href="/mats/inventory?t=tok">MATS</a>' in html
    assert 'class="keep-hero"' in html
    assert 'class="keep-class-grid"' in html
    assert 'class="keep-engagement-grid"' not in html
    assert 'name="eid"' in html
    assert "Open keep" in html
    assert "Paste upload" in html
    assert "Disk drop + rescan" in html
    assert "architecture" in html


def test_mats_inventory_is_barracks_known_keeps_card_view(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    em.add_mat_bytes(
        state,
        "acme-engagement",
        "architecture",
        "overview.md",
        b"system map",
        uploaded_by="operator",
        tags=["seed"],
    )

    html = mats_views.render_mats_inventory(state, "tok")

    assert '<div class="ara-eyebrow">Barracks</div>' in html
    assert "<title>ARA · MATS</title>" in html
    assert '<a href="/mats?t=tok">KEEP</a>' in html
    assert '<a href="/mats/inventory?t=tok" class="active">MATS</a>' in html
    assert 'class="keep-engagement-grid"' in html
    assert "Known keeps" in html
    assert "acme-engagement" in html
    assert "architecture" in html


def test_mats_detail_groups_management_upload_and_deletion(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    em.add_mat_bytes(
        state,
        "acme-engagement",
        "source",
        "app.py",
        b"print('ara')",
        uploaded_by="forge:demo",
        tags=["forge"],
        note="bridged source",
    )
    em.add_mat_bytes(
        state,
        "acme-engagement",
        "creds",
        "token.txt",
        b"secret",
    )
    assert em.set_mat_selected(
        state, "acme-engagement", "creds/token.txt", False,
    ) is True

    html = mats_views.render_mats_detail(state, "acme-engagement", "tok")

    assert '<div class="ara-eyebrow">Keep</div>' in html
    assert 'class="keep-hero"' in html
    assert 'class="keep-layout"' in html
    assert 'class="keep-upload-panel"' in html
    assert 'enctype="multipart/form-data"' in html
    assert 'type="file" name="file"' in html
    assert 'action="/mats/acme-engagement/upload?t=tok"' in html
    assert 'class="keep-class-section"' in html
    assert html.count('class="keep-class-section"') == len(em.MAT_CLASSES)
    assert 'class="keep-mat-row"' in html
    assert 'action="/mats/acme-engagement/paste?t=tok"' in html
    assert 'action="/mats/acme-engagement/rescan?t=tok"' in html
    assert "/delete?t=tok" in html
    assert ">Delete file</button>" in html
    assert "/exclude?t=tok" in html
    assert "/include?t=tok" in html
    assert "Exclude from generation" in html
    assert "Use for generation" in html
    assert 'class="keep-file-meta"' in html
    assert 'class="keep-mat-actions"' in html
    assert "indexed" in html
    assert "used" in html
    assert "excluded" in html
    assert "12 B" in html
    assert "Python" in html
    assert "6 B" in html
    assert "Text" in html
    assert "Paste upload" in html
    assert "Disk organization" in html
    assert "Raw manifest" in html
    assert "app.py" in html
    assert "token.txt" in html


def test_mats_file_upload_handler_adds_uploaded_file(tmp_path: Path) -> None:
    state = tmp_path / ".ara"

    target = mats_views.handle_mats_upload(
        state,
        "acme-engagement",
        {
            "mat_class": "docs",
            "filename": "brief.md",
            "file": b"# Brief\nUploaded from browser.\n",
            "uploaded_by": "operator",
            "note": "browser upload",
        },
        "tok",
    )

    assert target.startswith("/mats/acme-engagement?t=tok")
    assert "added%20docs/brief.md" in target
    recs = em.list_mats(state, "acme-engagement")
    assert len(recs) == 1
    assert recs[0].path == "docs/brief.md"
    assert recs[0].uploaded_by == "operator"
    assert recs[0].note == "browser upload"
