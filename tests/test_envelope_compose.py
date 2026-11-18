from __future__ import annotations

from ara import _rhema_shim as rh


_DEFAULT_SYSTEM = (
    "You are Hermes Agent, a helpful assistant by Nous Research.\n\n"
    "# Finishing the job\n"
    "NEVER substitute plausible-looking fabricated output; report a blocker "
    "honestly.\n\n"
    "# Tool-use enforcement\n"
    "You MUST use your tools to take action.\n"
)


def _envelope(body: dict, keep: list | None = None) -> dict:
    spec = {"op": "apply_envelope", "text": "ARMOR ABSOLUTE TEXT"}
    if keep is not None:
        spec["keep_default_sections"] = keep
    return rh.apply_rewrite_spec(body, spec)


def test_envelope_full_replace_is_default() -> None:
    body = {"messages": [{"role": "system", "content": _DEFAULT_SYSTEM},
                         {"role": "user", "content": "hi"}]}
    out = _envelope(body)
    sys_msg = out["messages"][0]["content"]
    # default: absolute fully replaces the default Hermes prompt
    assert sys_msg == "ARMOR ABSOLUTE TEXT"
    assert "Nous Research" not in sys_msg


def test_envelope_keeps_opted_in_default_section() -> None:
    body = {"messages": [{"role": "system", "content": _DEFAULT_SYSTEM},
                         {"role": "user", "content": "hi"}]}
    out = _envelope(body, keep=["Finishing the job"])
    sys_msg = out["messages"][0]["content"]
    # kept section is composed in; the absolute still present
    assert "# Finishing the job" in sys_msg
    assert "fabricated output" in sys_msg
    assert "ARMOR ABSOLUTE TEXT" in sys_msg
    # non-kept default section is dropped
    assert "Tool-use enforcement" not in sys_msg
    assert "Nous Research" not in sys_msg


def test_envelope_keep_miss_falls_back_to_replace() -> None:
    body = {"messages": [{"role": "system", "content": _DEFAULT_SYSTEM}]}
    out = _envelope(body, keep=["No Such Section"])
    assert out["messages"][0]["content"] == "ARMOR ABSOLUTE TEXT"


def test_envelope_instructions_field_compose() -> None:
    body = {"instructions": _DEFAULT_SYSTEM}
    out = _envelope(body, keep=["finishing the job"])
    assert "fabricated output" in out["instructions"]
    assert "ARMOR ABSOLUTE TEXT" in out["instructions"]
