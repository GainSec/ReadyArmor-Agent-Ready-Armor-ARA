from __future__ import annotations

from pathlib import Path

from ara.mitm_proxy import MitmProxy
from ara.runtime.binder.api import EvidenceBinder


def test_mitm_record_captures_bounded_evidence(tmp_path: Path) -> None:
    session = tmp_path / "sessions" / "s1"
    session.mkdir(parents=True)
    proxy = MitmProxy(tmp_path, session)

    proxy._record(
        "example.test",
        443,
        {
            "start": "GET /login HTTP/1.1",
            "headers": {"host": "example.test"},
        },
        b"",
        {
            "start": "HTTP/1.1 200 OK",
            "headers": {"content-type": "text/html"},
        },
        b"<html>Example login form csrf_token</html>",
    )

    binder = EvidenceBinder(session)
    results = binder.search("csrf_token")

    assert results
    rec = results[0].record
    assert rec.source == "mitm:request"
    assert rec.source_metadata["url"] == "https://example.test:443/login"
    assert rec.source_metadata["method"] == "GET"
    assert rec.source_metadata["status"] == 200
