from __future__ import annotations

import json
from pathlib import Path

from ara import approvals
from ara import resources
from ara.armory import views


def test_resource_without_locator_needs_resolution() -> None:
    out = resources.normalize_resource_intent(
        {
            "class": "wordlist",
            "name": "SecLists",
            "purpose": "web app recon wordlists",
            "hints": ["SecLists", "danielmiessler", "GitHub"],
        },
        citation={"manifest_element_id": "elem_1"},
        extraction_id="x_res_1",
    )

    assert out["id"] == "res_seclists"
    assert out["class"] == "wordlist"
    assert out["status"] == "needs_resolution"
    assert out["locator"] == ""
    assert out["hints"] == ["SecLists", "danielmiessler", "GitHub"]
    assert out["allowed_uses"] == ["reference", "fetch"]
    assert out["requires_approval_for"] == [
        "execute",
        "scope_expansion",
        "aggressive_tooling",
    ]
    assert out["citation"] == {"manifest_element_id": "elem_1"}
    assert out["extraction_id"] == "x_res_1"


def test_resource_with_http_locator_is_resolved() -> None:
    out = resources.normalize_resource_intent(
        {
            "class": "reference",
            "name": "OWASP WSTG",
            "locator": "https://owasp.org/www-project-web-security-testing-guide/",
        },
        citation={"manifest_element_id": "elem_2"},
        extraction_id="x_res_2",
    )

    assert out["status"] == "resolved"
    assert out["locator"] == "https://owasp.org/www-project-web-security-testing-guide/"
    assert out["resolution"]["source"] == "mat_locator"


def test_resource_with_failed_probe_needs_operator_resolution() -> None:
    out = resources.normalize_resource_intent(
        {
            "class": "wordlist",
            "name": "SecLists",
            "locator": "https://old.example.invalid/SecLists",
            "resolution": {
                "source": "probe",
                "status_code": 403,
                "error": "forbidden",
            },
        },
        citation={"manifest_element_id": "elem_3"},
        extraction_id="x_res_3",
    )

    assert out["status"] == "probe_failed"
    assert resources.needs_operator_resolution(out) is True
    assert out["resolution"]["status_code"] == 403
    assert out["resolution"]["error"] == "forbidden"


def test_resource_policy_rules_compile_resolved_http_only() -> None:
    resolved = resources.normalize_resource_intent(
        {
            "id": "res_seclists",
            "class": "wordlist",
            "name": "SecLists",
            "locator": "https://github.com/danielmiessler/SecLists",
        },
        citation={"manifest_element_id": "elem_4"},
        extraction_id="x_res_4",
    )
    unresolved = resources.normalize_resource_intent(
        {"id": "res_payloads", "class": "wordlist", "name": "Payload list"},
        citation={"manifest_element_id": "elem_5"},
        extraction_id="x_res_5",
    )

    text = resources.render_resource_bulwark_text([resolved, unresolved])

    assert "resource:res_seclists" in text
    assert "allow    net   github.com:443" in text
    assert "allow    url   https://github.com/danielmiessler/SecLists" in text
    assert "res_payloads" not in text


def test_resolve_resource_intent_uses_search_and_probe() -> None:
    resource = resources.normalize_resource_intent(
        {
            "class": "wordlist",
            "name": "SecLists",
            "hints": ["SecLists", "GitHub"],
        },
    )

    out = resources.resolve_resource_intent(
        resource,
        search=lambda query: ["https://github.com/danielmiessler/SecLists"],
        probe=lambda url: resources.ResourceProbe(
            url=url,
            status_code=200,
            final_url=url,
        ),
    )

    assert out["status"] == "resolved"
    assert out["locator"] == "https://github.com/danielmiessler/SecLists"
    assert out["resolution"]["source"] == "web_search"
    assert out["resolution"]["status_code"] == 200


def test_resolve_resource_intent_records_probe_failure() -> None:
    resource = resources.normalize_resource_intent(
        {
            "class": "wordlist",
            "name": "Legacy Wordlists",
            "hints": ["legacy wordlists"],
        },
    )

    out = resources.resolve_resource_intent(
        resource,
        search=lambda query: ["https://example.invalid/old-wordlists"],
        probe=lambda url: resources.ResourceProbe(
            url=url,
            status_code=403,
            error="forbidden",
        ),
    )

    assert out["status"] == "probe_failed"
    assert out["locator"] == "https://example.invalid/old-wordlists"
    assert out["resolution"]["source"] == "web_search"
    assert out["resolution"]["status_code"] == 403
    assert out["resolution"]["error"] == "forbidden"
    assert resources.needs_operator_resolution(out) is True


def test_write_resource_url_approval_and_apply_operator_url(tmp_path: Path) -> None:
    session = tmp_path / ".ara" / "sessions" / "s1"
    resource = resources.normalize_resource_intent(
        {"id": "res_seclists", "class": "wordlist", "name": "SecLists"},
    )

    rec = resources.write_resource_url_approval(
        session,
        resource,
        suggested_url="https://example.invalid/SecLists",
    )
    loaded = approvals.read_pending(session, rec.id)
    assert loaded is not None
    assert loaded.op == "resource.resolve_url"
    assert loaded.resource_id == "res_seclists"
    assert loaded.resource_name == "SecLists"
    assert loaded.suggested_url == "https://example.invalid/SecLists"

    resolved = approvals.resolve(
        session,
        rec.id,
        "allow",
        resolved_url="https://github.com/danielmiessler/SecLists",
    )
    assert resolved is not None
    out = resources.apply_resource_resolution(resource, resolved)
    assert out["status"] == "resolved"
    assert out["locator"] == "https://github.com/danielmiessler/SecLists"
    assert out["resolution"]["source"] == "operator"


def test_apply_operator_url_updates_matching_weapon_resource(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    wdir = state / "weapons" / "w1"
    wdir.mkdir(parents=True)
    (wdir / "seal.json").write_text("{}")
    (wdir / "weapon.json").write_text(
        json.dumps(
            {
                "weapon_target_id": "w1",
                "resources": [
                    {
                        "id": "res_seclists",
                        "class": "wordlist",
                        "name": "SecLists",
                        "status": "needs_resolution",
                    }
                ],
            }
        )
    )
    session = state / "sessions" / "resource-resolution"
    rec = resources.write_resource_url_approval(
        session,
        {
            "id": "res_seclists",
            "class": "wordlist",
            "name": "SecLists",
        },
        weapon_id="w1",
    )
    resolved = approvals.resolve(
        session,
        rec.id,
        "allow",
        resolved_url="https://github.com/danielmiessler/SecLists",
    )
    assert resolved is not None

    assert resources.apply_resource_resolution_to_matching_weapons(
        state, resolved,
    ) == 1
    data = json.loads((wdir / "weapon.json").read_text())
    assert data["resources"][0]["status"] == "resolved"
    assert data["resources"][0]["locator"] == (
        "https://github.com/danielmiessler/SecLists"
    )
    assert data["resources"][0]["resolution"]["source"] == "operator"


def test_courtyard_approval_renders_resource_url_input(tmp_path: Path) -> None:
    state = tmp_path / ".ara"
    session = state / "sessions" / "s1"
    resource = resources.normalize_resource_intent(
        {"id": "res_seclists", "class": "wordlist", "name": "SecLists"},
    )
    resources.write_resource_url_approval(
        session,
        resource,
        suggested_url="https://example.invalid/SecLists",
    )

    html = views.render_courtyard(state, "tok", tab="approvals")

    assert "resource.resolve_url" in html
    assert 'name="resolved_url"' in html
    assert "Enter usable URL" in html
    assert "res_seclists" in html
