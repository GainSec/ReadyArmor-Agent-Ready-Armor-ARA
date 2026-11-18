from __future__ import annotations

import pytest

from ara import temper_controls


def test_rhema_control_requires_rule_shape() -> None:
    control = {
        "action": "rewrite",
        "layer": "reply",
        "pattern": "regex:PRIVATE KEY",
        "replacement": "[REDACTED_SECRET]",
        "rationale": "MATS allow presence/risk reporting, not raw key bytes.",
    }

    normalized = temper_controls.validate_control("rhema", control)

    assert normalized == control


def test_rhema_rewrite_control_requires_replacement() -> None:
    with pytest.raises(temper_controls.TemperControlError, match="replacement"):
        temper_controls.validate_control("rhema", {
            "action": "rewrite",
            "layer": "reply",
            "pattern": "regex:PRIVATE KEY",
            "rationale": "MATS require secret redaction.",
        })


def test_bulwark_control_renders_enforceable_rule() -> None:
    rendered = temper_controls.render_control(
        "bulwark",
        {
            "action": "approval",
            "layer": "fs",
            "match": "/etc/**",
            "condition": "task_family == web_app",
            "exception_path": (
                "Allow with matching host/system trim, matching weapon, "
                "or explicit operator approval."
            ),
        },
        citations=[{"manifest_element_id": "elem_wstg"}],
    )

    assert "approval fs /etc/**" in rendered
    assert "# condition: task_family == web_app" in rendered
    assert "# exception:" in rendered
    assert "# cite: elem_wstg" in rendered


def test_bulwark_regex_alternation_is_normalized_to_fire() -> None:
    # The model emits regex alternation WITHOUT the `regex:` prefix bulwark
    # needs. Left raw, bulwark re.escapes it and the rule never fires. Repair
    # it at validation time so it materializes as an enforceable rule.
    from ara import bulwark

    control = {
        "action": "deny",
        "layer": "exec",
        "match": "jtag|uart|swd|openocd|flashrom|esptool",
        "exception_path": "Operator activates hardware trim.",
    }
    normalized = temper_controls.validate_control("bulwark", control)
    assert normalized["match"] == "regex:jtag|uart|swd|openocd|flashrom|esptool"

    rendered = temper_controls.render_control(
        "bulwark", control, citations=[{"manifest_element_id": "elem_hw"}],
    )
    # End-to-end through the real bulwark engine (rules need a scope heading).
    bw = bulwark.parse("## global\n" + rendered)
    assert bw.rules, "normalized rule must register"
    deny = bulwark.evaluate(
        bw, {"layer": "exec", "path": "esptool", "argv": "write_flash fw.bin"})
    allow = bulwark.evaluate(
        bw, {"layer": "exec", "path": "ls", "argv": "-la"})
    assert deny.action == "deny"   # grounded rule now blocks the hardware tool
    assert allow.action == "allow"  # ... while leaving normal commands alone


def test_bulwark_literal_match_left_untouched() -> None:
    # A plain literal token already fires under bulwark's default (non-regex)
    # matching, so it must NOT be rewritten into a regex.
    normalized = temper_controls.validate_control("bulwark", {
        "action": "deny", "layer": "exec", "match": "esptool",
        "exception_path": "Operator approval.",
    })
    assert normalized["match"] == "esptool"


def test_absolute_control_renders_methodology_context_without_generic_heading() -> None:
    rendered = temper_controls.render_control(
        "absolute",
        {
            "principle": "methodology_fit",
            "allowed": ["browser automation", "HTTP request review"],
            "restricted": ["binwalk", "flashrom"],
            "exception_path": "Require firmware trim or operator approval.",
            "methodology_context": {
                "task_family": "web_app",
                "expected": ["browser automation", "HTTP request review"],
                "unexpected": ["binwalk", "flashrom"],
                "exception_path": "Require firmware trim or operator approval.",
            },
        },
    )

    assert "## Tempered Armor: Methodology-fit boundary" in rendered
    assert "## Tempered armor principle" not in rendered
    assert "Applies when: `web_app` methodology context." in rendered
    assert "- Restricted for this context:" in rendered
    assert "browser automation" in rendered


def test_thread_type_is_limited_to_tailor_temper_threads() -> None:
    assert temper_controls.validate_thread_type("safety_boundary") == "safety_boundary"

    with pytest.raises(temper_controls.TemperControlError, match="thread_type"):
        temper_controls.validate_thread_type("weapon_forge")


# --- D5: rhema pattern-quality gate -------------------------------------

import pytest as _pytest

from ara import temper_controls as _tc


@_pytest.mark.parametrize("pattern", [
    "regex:PRIVATE KEY",
    "regex:Bearer [A-Za-z0-9._-]{20,}",
    "regex:-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "regex:something",  # prefixed → trusted as a regex
])
def test_validate_rhema_pattern_accepts_targeted_matchers(pattern: str) -> None:
    _tc.validate_rhema_pattern(pattern)  # no raise


@_pytest.mark.parametrize("pattern,needle", [
    ("exploit_payload", "bare label"),                 # dead non-prefixed label
    ("web_app_config_secrets", "bare label"),
    (r"https?://[\w.-]+", "over-broad"),               # redacts every URL
    ("/etc/passwd|/proc/self|hostname", "over-broad"),  # 'hostname' hits prose
    ("regex:(unbalanced", "not a valid regex"),
    ("regex:.*", "empty string"),                       # matches everything
    ("", "empty"),
])
def test_validate_rhema_pattern_rejects_bad_patterns(pattern: str, needle: str) -> None:
    with _pytest.raises(_tc.TemperControlError, match=needle):
        _tc.validate_rhema_pattern(pattern)


def test_bulwark_comma_token_list_becomes_regex_alternation() -> None:
    normalized = temper_controls.validate_control("bulwark", {
        "action": "deny", "layer": "exec",
        "match": "adb, fastboot, esptool", "exception_path": "op"})
    assert normalized["match"] == "regex:adb|fastboot|esptool"


def test_bulwark_prose_match_is_rejected() -> None:
    with pytest.raises(temper_controls.TemperControlError, match="prose"):
        temper_controls.validate_control("bulwark", {
            "action": "deny", "layer": "exec",
            "match": "hardcoded credentials in source code",
            "exception_path": "op"})


def test_bulwark_single_token_and_regex_still_pass() -> None:
    a = temper_controls.validate_control("bulwark", {
        "action": "deny", "layer": "exec", "match": "esptool",
        "exception_path": "op"})
    assert a["match"] == "esptool"
    b = temper_controls.validate_control("bulwark", {
        "action": "deny", "layer": "exec", "match": "regex:jtag|uart debug",
        "exception_path": "op"})
    assert b["match"] == "regex:jtag|uart debug"   # regex: prefix -> not prose-rejected
