"""Tests for `ara tool` CLI subcommand."""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ara import tool_registry as tr
from ara.tool_cli import (
    cmd_approve,
    cmd_delete,
    cmd_list,
    cmd_override,
    cmd_reclassify,
    cmd_show,
    cmd_sweep_expired,
)


def _seed(state: Path) -> None:
    tr.record_classification(
        state, key="nmap | -sV*",
        classification=tr.Classification(active=True, intrusive=True, scanner=True),
        confidence=3, reasoning="version probe", engagement_id="acme",
    )
    tr.record_classification(
        state, key="rm | -rf*",
        classification=tr.Classification(destructive=True),
        confidence=2, reasoning="recursive deletion",
    )


def _ns(state_root: Path, **kw) -> argparse.Namespace:
    return argparse.Namespace(state_root=state_root, **kw)


def _run(fn, ns):
    out = io.StringIO()
    err = io.StringIO()
    with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
        rc = fn(ns)
    return rc, out.getvalue(), err.getvalue()


def test_list_prints_table_when_populated(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out, _ = _run(cmd_list, _ns(tmp_path, status=None))
    assert rc == 0
    assert "nmap" in out and "rm" in out
    assert "active" in out and "destructive" in out


def test_list_filters_by_status(tmp_path: Path) -> None:
    _seed(tmp_path)
    tr.operator_approve(tmp_path, "nmap | -sV*")
    rc, out, _ = _run(cmd_list, _ns(tmp_path, status=tr.STATUS_CONFIRMED))
    assert rc == 0
    assert "nmap" in out
    # 'rm' substring lives inside 'confirmed' so check for the row
    # signature instead — `rm | -rf*` is the rm row's flag pattern
    assert "-rf*" not in out


def test_list_empty_state(tmp_path: Path) -> None:
    rc, out, _ = _run(cmd_list, _ns(tmp_path, status=None))
    assert rc == 0
    assert "empty" in out


def test_show_dumps_full_record(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out, _ = _run(cmd_show, _ns(tmp_path, key="nmap | -sV*"))
    assert rc == 0
    assert '"key": "nmap | -sV*"' in out
    assert '"reasoning": "version probe"' in out


def test_show_missing_key(tmp_path: Path) -> None:
    rc, _, err = _run(cmd_show, _ns(tmp_path, key="ghost | *"))
    assert rc == 2
    assert "no entry" in err


def test_approve_flips_status(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out, _ = _run(cmd_approve, _ns(tmp_path, key="nmap | -sV*"))
    assert rc == 0
    e = tr.lookup(tmp_path, "nmap | -sV*")
    assert e is not None and e.status == tr.STATUS_CONFIRMED


def test_override_writes_classification(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out, _ = _run(cmd_override, _ns(
        tmp_path, key="nmap | -sV*",
        active=True, destructive=True,
        intrusive=False, scanner=False,
        reason="treats nmap as destructive on this customer",
    ))
    assert rc == 0
    e = tr.lookup(tmp_path, "nmap | -sV*")
    assert e is not None
    assert e.status == tr.STATUS_OPERATOR_OVERRIDDEN
    assert e.operator_override is not None
    assert e.operator_override.classification.destructive is True


def test_reclassify_sets_flag(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, _, _ = _run(cmd_reclassify, _ns(tmp_path, key="nmap | -sV*"))
    assert rc == 0
    e = tr.lookup(tmp_path, "nmap | -sV*")
    assert e is not None and e.reclassify_on_next_call is True


def test_delete_removes_entry(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, _, _ = _run(cmd_delete, _ns(tmp_path, key="nmap | -sV*"))
    assert rc == 0
    assert tr.lookup(tmp_path, "nmap | -sV*") is None


def test_delete_missing_key(tmp_path: Path) -> None:
    rc, _, err = _run(cmd_delete, _ns(tmp_path, key="ghost | *"))
    assert rc == 2
    assert "no entry" in err


def test_sweep_expired_no_op_when_nothing_expired(tmp_path: Path) -> None:
    _seed(tmp_path)
    rc, out, _ = _run(cmd_sweep_expired, _ns(tmp_path))
    assert rc == 0
    assert "no expired" in out


def test_sweep_expired_merges_past_ttl(tmp_path: Path) -> None:
    # Seed with a fixed timestamp so we control TTL
    tr.record_classification(
        tmp_path, key="x | *",
        classification=tr.Classification(active=True),
        confidence=2, reasoning="", now=1_000_000_000.0,
    )
    # Manually backdate the TTL so the sweep picks it up "now".
    e = tr.lookup(tmp_path, "x | *")
    assert e is not None
    e.ttl_expires_at = "2020-01-01T00:00:00Z"  # well in the past
    tr.save_entry(tmp_path, e)

    rc, out, _ = _run(cmd_sweep_expired, _ns(tmp_path))
    assert rc == 0
    assert "merged x | *" in out
    e2 = tr.lookup(tmp_path, "x | *")
    assert e2 is not None and e2.status == tr.STATUS_MERGED_BY_TTL
