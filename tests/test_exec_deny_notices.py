"""exec_deny_notices: one-shot queue + message composition + term extraction."""

from __future__ import annotations

from pathlib import Path

from ara import exec_deny_notices as edn


# --- queue append / drain ------------------------------------------------

def test_append_then_drain_returns_record_and_removes_file(tmp_path: Path) -> None:
    edn.append(
        tmp_path, rule_id="r188", matched_token="reboot",
        cmd_summary="bash -c '...reboot...'", ts=1.0,
    )
    assert (tmp_path / edn.QUEUE_NAME).exists()
    got = edn.drain(tmp_path)
    assert len(got) == 1
    assert got[0]["rule_id"] == "r188"
    assert got[0]["matched_token"] == "reboot"
    # One-shot: file gone after drain.
    assert not (tmp_path / edn.QUEUE_NAME).exists()


def test_drain_empty_when_no_queue(tmp_path: Path) -> None:
    assert edn.drain(tmp_path) == []


def test_drain_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / edn.QUEUE_NAME
    p.write_text('{"rule_id":"r1","matched_token":"halt"}\nnot json\n\n', encoding="utf-8")
    got = edn.drain(tmp_path)
    assert [n["matched_token"] for n in got] == ["halt"]
    assert not p.exists()  # removed even though a line was malformed


def test_drain_caps_records(tmp_path: Path) -> None:
    p = tmp_path / edn.QUEUE_NAME
    p.write_text("".join('{"rule_id":"r","matched_token":"x"}\n' for _ in range(edn._MAX_RECORDS + 50)), encoding="utf-8")
    assert len(edn.drain(tmp_path)) == edn._MAX_RECORDS


def test_append_never_raises_on_bad_dir(tmp_path: Path) -> None:
    bad = tmp_path / "does" / "not" / "exist"
    # Parent dirs absent -> open() fails -> swallowed, no raise.
    edn.append(bad, rule_id="r", matched_token="t", cmd_summary="c", ts=1.0)


# --- message composition -------------------------------------------------

def test_compose_single_token_names_term_and_rule() -> None:
    msg = edn.compose_message([{"rule_id": "r188", "matched_token": "reboot"}])
    assert "`reboot`" in msg
    assert "`r188`" in msg
    assert "on the term `reboot`" in msg
    assert "skip" in msg.lower()
    assert "out of scope" in msg.lower()


def test_compose_coalesces_distinct_tokens() -> None:
    msg = edn.compose_message([
        {"rule_id": "r188", "matched_token": "reboot"},
        {"rule_id": "r188", "matched_token": "reboot"},  # dup collapses
        {"rule_id": "r188", "matched_token": "poweroff"},
        {"rule_id": "r188", "matched_token": "halt"},
    ])
    assert "on the terms" in msg
    assert "`reboot`" in msg and "`poweroff`" in msg and "`halt`" in msg
    # reboot appears once despite two notices.
    assert msg.count("`reboot`") == 1


def test_compose_caps_tokens_with_and_more() -> None:
    notices = [{"rule_id": "r188", "matched_token": f"t{i}"} for i in range(edn._MAX_TOKENS_SHOWN + 3)]
    msg = edn.compose_message(notices)
    assert "…and 3 more" in msg


def test_compose_no_token_falls_back_to_rule_only() -> None:
    msg = edn.compose_message([{"rule_id": "r188", "matched_token": ""}])
    assert "`r188`" in msg
    assert "on the term" not in msg  # no dangling term clause


def test_compose_empty_returns_blank() -> None:
    assert edn.compose_message([]) == ""


# --- term extraction -----------------------------------------------------

_BY969C_RULE = (
    r"(?i)(\bwrite_flash\b|\berase_flash\b|factory[_-]?reset|"
    r"fastboot\s+(?:flash|erase|-w)|\bmkfs\b|\breboot\b|\bpoweroff\b|"
    r"\bhalt\b|\brm\s+-rf\b|\bshred\b|\bdd\b[^\n]*\bof=)"
)


def test_extract_from_real_scan_command_yields_reboot() -> None:
    cmd = "bash -c 'python3 -c \"for p in [\\'/admin\\',\\'/reboot\\',\\'/status\\']: get(p)\"'"
    assert edn.extract_matched_token(_BY969C_RULE, True, cmd) == "reboot"


def test_extract_no_match_returns_empty() -> None:
    cmd = "bash -c 'curl http://192.168.18.9/status'"
    assert edn.extract_matched_token(_BY969C_RULE, True, cmd) == ""


def test_extract_literal_rule() -> None:
    assert edn.extract_matched_token("/usr/bin/rm", False, "/usr/bin/rm -rf /") == "/usr/bin/rm"


def test_extract_empty_command() -> None:
    assert edn.extract_matched_token(_BY969C_RULE, True, "") == ""


def test_extract_bad_regex_never_raises() -> None:
    assert edn.extract_matched_token("(unclosed", True, "reboot now") == ""
