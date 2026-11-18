"""Forge-curated resource intent normalization and policy rendering."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from ara import approvals
from ara import weapons as weapons_mod


RESOURCE_CLASSES = {
    "reference",
    "wordlist",
    "script",
    "package",
    "repository",
    "vendor_doc",
    "vuln_db",
    "tool_doc",
}
RESOLUTION_STATUSES = {
    "resolved",
    "needs_resolution",
    "probe_failed",
    "ambiguous",
}
DEFAULT_ALLOWED_USES = ["reference", "fetch"]
DEFAULT_APPROVAL_USES = [
    "execute",
    "scope_expansion",
    "aggressive_tooling",
]


@dataclass
class ResourceProbe:
    url: str
    status_code: int | None = None
    final_url: str = ""
    error: str = ""


def _slug(value: object, *, fallback: str = "resource") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or fallback


def _listify(value: object, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [value] if value.strip() else list(default)
    if isinstance(value, list | tuple | set):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out or list(default)
    return list(default)


def _coerce_status_code(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def normalize_resource_intent(
    body: dict,
    *,
    citation: dict | None = None,
    extraction_id: str | None = None,
) -> dict:
    """Return a stable resource-intent record from an MCP extraction body."""
    if not isinstance(body, dict):
        body = {}

    name = str(body.get("name") or "").strip()
    locator = str(
        body.get("locator")
        or body.get("url")
        or body.get("repo")
        or body.get("repository")
        or ""
    ).strip()
    rid = str(body.get("id") or "").strip()
    if not rid:
        rid = "res_" + _slug(name or locator or body.get("class"))

    cls = str(body.get("class") or body.get("resource_class") or "reference").strip()
    cls = _slug(cls, fallback="reference")
    if cls not in RESOURCE_CLASSES:
        cls = "reference"

    resolution = dict(body.get("resolution") or {})
    status_code = _coerce_status_code(
        resolution.get("status_code", body.get("status_code"))
    )
    if status_code is not None:
        resolution["status_code"] = status_code

    requested_status = str(body.get("status") or "").strip().lower()
    parsed = urlparse(locator)
    if status_code is not None and status_code >= 400:
        status = "probe_failed"
    elif requested_status in RESOLUTION_STATUSES:
        status = requested_status
    elif parsed.scheme in {"http", "https"} and parsed.hostname:
        status = "resolved"
    else:
        status = "needs_resolution"

    if locator and status == "resolved" and not resolution.get("source"):
        resolution["source"] = "mat_locator"

    out = {
        "id": rid,
        "class": cls,
        "name": name or rid,
        "locator": locator,
        "status": status,
        "hints": _listify(body.get("hints"), []),
        "allowed_uses": _listify(body.get("allowed_uses"), DEFAULT_ALLOWED_USES),
        "requires_approval_for": _listify(
            body.get("requires_approval_for"),
            DEFAULT_APPROVAL_USES,
        ),
        "resolution": resolution,
    }
    for key in ("purpose", "rationale", "description", "notes"):
        value = body.get(key)
        if value:
            out[key] = str(value).strip()
    if citation is not None:
        out["citation"] = citation
    elif body.get("citation") is not None:
        out["citation"] = body["citation"]
    if extraction_id is not None:
        out["extraction_id"] = extraction_id
    elif body.get("extraction_id") is not None:
        out["extraction_id"] = body["extraction_id"]
    return out


def needs_operator_resolution(resource: dict) -> bool:
    return str(resource.get("status") or "") in {
        "needs_resolution",
        "probe_failed",
        "ambiguous",
    }


def _resource_query(resource: dict) -> str:
    parts = [
        str(resource.get("name") or ""),
        str(resource.get("class") or ""),
        " ".join(str(h) for h in list(resource.get("hints") or [])),
    ]
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


def _first_http_url(candidates: list[str]) -> str:
    for candidate in candidates:
        text = str(candidate or "").strip()
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return text
    return ""


def resolve_resource_intent(
    resource: dict,
    *,
    search: Callable[[str], list[str]],
    probe: Callable[[str], ResourceProbe],
) -> dict:
    """Resolve a resource intent via injected search/probe callables.

    This function is deliberately pure from ARA's perspective: callers decide
    whether `search` means web search, cached lookup, or a local registry.
    Only a successfully probed HTTP(S) URL becomes policy material.
    """
    out = normalize_resource_intent(dict(resource or {}))
    if out.get("status") == "resolved":
        return out
    query = _resource_query(out)
    candidates = search(query) if query else []
    url = _first_http_url(candidates)
    if not url:
        out["status"] = "needs_resolution"
        out["resolution"] = {
            **dict(out.get("resolution") or {}),
            "source": "web_search",
            "error": "no_http_candidate",
            "query": query,
        }
        return out
    result = probe(url)
    final_url = result.final_url or result.url or url
    out["locator"] = final_url
    resolution = {
        **dict(out.get("resolution") or {}),
        "source": "web_search",
        "query": query,
    }
    if result.status_code is not None:
        resolution["status_code"] = result.status_code
    if result.error:
        resolution["error"] = result.error
    out["resolution"] = resolution
    if result.status_code is not None and result.status_code >= 400:
        out["status"] = "probe_failed"
    else:
        parsed = urlparse(final_url)
        out["status"] = (
            "resolved"
            if parsed.scheme in {"http", "https"} and parsed.hostname
            else "needs_resolution"
        )
    return out


def write_resource_url_approval(
    session_root: Path,
    resource: dict,
    *,
    suggested_url: str = "",
    weapon_id: str = "",
) -> approvals.ApprovalRecord:
    rid = str(resource.get("id") or "").strip()
    name = str(resource.get("name") or rid or "resource").strip()
    rec = approvals.ApprovalRecord(
        id=approvals.new_approval_id(),
        session_id=Path(session_root).name,
        created_at=time.time(),
        op="resource.resolve_url",
        path=suggested_url or str(resource.get("locator") or ""),
        resource_id=rid,
        resource_name=name,
        resource_weapon_id=weapon_id or None,
        suggested_url=suggested_url or str(resource.get("locator") or ""),
        reason="Resource needs a current operator-approved URL.",
    )
    approvals.write_pending(Path(session_root), rec)
    return rec


def apply_resource_resolution(
    resource: dict,
    approval: approvals.ApprovalRecord,
) -> dict:
    out = normalize_resource_intent(dict(resource or {}))
    if approval.status != "allowed" or not approval.resolved_url:
        return out
    parsed = urlparse(approval.resolved_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        out["status"] = "needs_resolution"
        out["resolution"] = {
            **dict(out.get("resolution") or {}),
            "source": "operator",
            "error": "invalid_operator_url",
        }
        return out
    out["locator"] = approval.resolved_url
    out["status"] = "resolved"
    out["resolution"] = {
        **dict(out.get("resolution") or {}),
        "source": "operator",
        "approved_at": approval.resolved_at,
    }
    return out


def apply_resource_resolution_to_weapon(
    state_root: Path,
    weapon_id: str,
    resource_id: str,
    approval: approvals.ApprovalRecord,
) -> bool:
    wpath = weapons_mod.weapon_dir(Path(state_root), weapon_id) / "weapon.json"
    if not wpath.is_file():
        return False
    try:
        import json
        data = json.loads(wpath.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    resources = data.get("resources")
    if not isinstance(resources, list):
        return False
    changed = False
    out_resources: list = []
    for item in resources:
        if not isinstance(item, dict):
            out_resources.append(item)
            continue
        if str(item.get("id") or "") != resource_id:
            out_resources.append(item)
            continue
        updated = apply_resource_resolution(item, approval)
        out_resources.append(updated)
        changed = changed or updated != item
    if not changed:
        return False
    data["resources"] = out_resources
    tmp = wpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(wpath)
    return True


def apply_resource_resolution_to_matching_weapons(
    state_root: Path,
    approval: approvals.ApprovalRecord,
) -> int:
    resource_id = approval.resource_id or ""
    if not resource_id:
        return 0
    weapon_ids = (
        [approval.resource_weapon_id]
        if approval.resource_weapon_id else
        weapons_mod.list_sealed_weapons(Path(state_root))
    )
    count = 0
    for weapon_id in weapon_ids:
        if weapon_id and apply_resource_resolution_to_weapon(
            Path(state_root), weapon_id, resource_id, approval,
        ):
            count += 1
    return count


def render_resource_bulwark_text(resources: list[dict]) -> str:
    """Compile resolved HTTP(S) resources into scoped Bulwark allow rules."""
    if not resources:
        return ""
    lines = [
        "# Weapon resources — synthesized from operator mats",
        "#",
        "# These allow rules are scoped to resources sealed into weapon.json.",
        "# They are not target-scope expansion. Execution or aggressive use",
        "# of fetched content still requires an approval rule/decision.",
        "",
        "## armor: weapon",
        "",
    ]
    emitted = 0
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if resource.get("status") != "resolved":
            continue
        locator = str(resource.get("locator") or "").strip()
        parsed = urlparse(locator)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        rid = str(resource.get("id") or resource.get("name") or "resource").strip()
        name = str(resource.get("name") or rid).strip()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        url_match = locator.rstrip("/")
        comment = f"# resource:{rid}"
        if name and name != rid:
            comment += f" {name}"
        lines.extend([
            comment,
            f"allow    net   {parsed.hostname}:{port}",
            f"allow    url   {url_match}",
            "",
        ])
        emitted += 1
    if not emitted:
        return ""
    return "\n".join(lines).rstrip() + "\n"
