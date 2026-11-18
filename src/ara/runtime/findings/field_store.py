"""Per-session content-addressed store for finding FIELD objects (Proposal 2).

Each assertion inside a finding — its impact, remediation, confirmation claim,
reason, and each affected asset — can be stored as its own immutable, hashed,
addressable object, the same way evidence captures are (``e:`` ids). A field
object gets an id like ``impact:9f1c…`` and is idempotent by content hash:
writing the same text twice returns the same id. A rewrite mints a NEW object
and repoints the finding's map entry, leaving the prior object intact for
diff/history.

This layer is ADDITIVE: findings still inline their values (so every reader keeps
working); the field objects + the per-finding map are a parallel provenance /
tamper-evidence / diff layer. Repointing findings to reference-only is a
follow-on.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ara.runtime.binder.store import ContentStore

_FIELD_KIND = {
    "impact": "impact",
    "remediation": "remediation",
    "confirmation": "claim",
    "reason": "reason",
}


class FieldStore:
    def __init__(self, session_dir: "Path | str") -> None:
        root = Path(session_dir) / "fields"
        root.mkdir(parents=True, exist_ok=True)
        self._store = ContentStore(root / "sha256")
        self._index = root / "index.jsonl"       # {id, sha, kind, len}
        self._map = root / "finding_field_map.jsonl"  # {finding_id, refs, at}

    def put(self, kind: str, text: str) -> str:
        """Store one field value; return its content-addressed id (or "")."""
        text = str(text or "")
        if not text.strip():
            return ""
        sha, _ = self._store.put(text.encode("utf-8"))
        fid = f"{kind}:{sha[:16]}"
        with self._index.open("a") as fh:
            fh.write(json.dumps(
                {"id": fid, "sha": sha, "kind": kind, "len": len(text)},
                sort_keys=True) + "\n")
        return fid

    def _sha_for(self, field_id: str) -> "str | None":
        if not self._index.is_file():
            return None
        found = None
        for line in self._index.open():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("id") == field_id:
                found = r.get("sha")
        return found

    def get(self, field_id: str) -> "str | None":
        sha = self._sha_for(field_id)
        if not sha:
            return None
        try:
            return self._store.get(sha).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None

    def record_finding_fields(self, finding_id: str, fields: dict) -> dict:
        """``fields`` = {field_name: text | list}. Stores each as a field object
        and appends a map entry. Returns {field_name: id | [ids]}."""
        refs: dict = {}
        for name, val in fields.items():
            if name == "affected_assets":
                ids = [self.put("asset", a) for a in (val or [])
                       if str(a).strip()]
                ids = [i for i in ids if i]
                if ids:
                    refs[name] = ids
            else:
                fid = self.put(str(_FIELD_KIND.get(name, name)), val)
                if fid:
                    refs[name] = fid
        with self._map.open("a") as fh:
            fh.write(json.dumps(
                {"finding_id": finding_id, "refs": refs, "at": time.time()},
                sort_keys=True) + "\n")
        return refs

    def refs_for(self, finding_id: str) -> dict:
        """The latest field-ref map for a finding (last write wins)."""
        latest: dict = {}
        if self._map.is_file():
            for line in self._map.open():
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if r.get("finding_id") == finding_id:
                    latest = r.get("refs") or {}
        return latest

    def verify(self) -> dict:
        """Every field id in the map resolves to stored content with a matching
        hash. Returns {total, ok, missing:[ids]}."""
        ids: set = set()
        if self._map.is_file():
            for line in self._map.open():
                try:
                    refs = (json.loads(line).get("refs") or {})
                except Exception:  # noqa: BLE001
                    continue
                for v in refs.values():
                    ids.update(v if isinstance(v, list) else [v])
        missing = [i for i in ids if self.get(i) is None]
        return {"total": len(ids), "ok": len(ids) - len(missing),
                "missing": missing}
