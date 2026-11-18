"""Phase 2 — channel detection + enforcement gating.

`detect_channel` walks the request body looking for openclaw chat
metadata. `armor_applies_to_channel` decides whether an armor's policy
should fire for a turn classified to a given channel. `active_absolute_rule`
takes a channel_id and returns None when the equipped armor's
channels don't match.
"""

from __future__ import annotations

from pathlib import Path

from ara import armors as armors_mod
from ara import rhema as rhema_mod


# --- detect_channel -------------------------------------------------------


def test_detect_channel_explicit_discord_prefix():
    body = {
        "input": [
            {"role": "user", "content": [
                {"text": '{"chat_id": "discord:user:123"} hello', "type": "input_text"},
            ]},
        ],
    }
    assert armors_mod.detect_channel(body) == "discord"


def test_detect_channel_explicit_imessage_prefix():
    body = {"input": [{"role": "user", "content": [
        {"text": '{"chat_id": "imessage:+15555550100"}', "type": "input_text"},
    ]}]}
    assert armors_mod.detect_channel(body) == "imessage"


def test_detect_channel_explicit_telegram_prefix():
    body = {"input": [{"role": "user", "content": [
        {"text": '{"chat_id": "tg:12345"}', "type": "input_text"},
    ]}]}
    assert armors_mod.detect_channel(body) == "telegram"


def test_detect_channel_signal_prefix():
    body = {"input": [{"role": "user", "content": [
        {"text": '{"chat_id": "signal:+15555550100"}', "type": "input_text"},
    ]}]}
    assert armors_mod.detect_channel(body) == "signal"


def test_detect_channel_discord_snowflake_heuristic():
    """Real openclaw clawdbot emits ``"chat_id": "user:NNN..."`` with a
    Discord snowflake. No explicit prefix; we still classify it."""
    body = {"input": [{"role": "user", "content": [
        {"text": (
            'Conversation info (untrusted metadata):\n'
            '```json\n{"chat_id": "user:747137111114907748", '
            '"sender": "GainSec"}\n```\n\nYo'
        ), "type": "input_text"}
    ]}]}
    assert armors_mod.detect_channel(body) == "discord"


def test_detect_channel_returns_none_on_no_signal():
    body = {"input": [{"role": "user", "content": [
        {"text": "just a plain message no metadata", "type": "input_text"},
    ]}]}
    assert armors_mod.detect_channel(body) is None


def test_detect_channel_handles_non_dict_body():
    assert armors_mod.detect_channel(None) is None
    assert armors_mod.detect_channel("string") is None


# --- armor_applies_to_channel --------------------------------------------


def test_global_armor_applies_to_any_channel(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "g", description="t")
    armor = armors_mod.load_armor(state, "g")
    assert armors_mod.armor_applies_to_channel(armor.meta, "discord") is True
    assert armors_mod.armor_applies_to_channel(armor.meta, "telegram") is True
    assert armors_mod.armor_applies_to_channel(armor.meta, None) is True


def test_scoped_armor_only_applies_to_matching_channel(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "d", description="t")
    armors_mod.set_channels(state, "d", ["discord"])
    armor = armors_mod.load_armor(state, "d")
    assert armors_mod.armor_applies_to_channel(armor.meta, "discord") is True
    assert armors_mod.armor_applies_to_channel(armor.meta, "telegram") is False
    assert armors_mod.armor_applies_to_channel(armor.meta, None) is False


# --- active_absolute_rule with channel_id --------------------------------


def test_active_absolute_rule_fires_for_global_armor(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "g", description="t")
    armor = armors_mod.load_armor(state, "g")
    armors_mod.path_for_absolute(armor, "default").write_text("X")
    armors_mod.set_active_armor(state, "g")
    # No channels set on armor → applies regardless.
    assert rhema_mod.active_absolute_rule(state, channel_id="discord") is not None
    assert rhema_mod.active_absolute_rule(state, channel_id=None) is not None


def test_active_absolute_rule_skipped_when_channel_does_not_match(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    armors_mod.create_armor(state, "d", description="t")
    armor = armors_mod.load_armor(state, "d")
    armors_mod.path_for_absolute(armor, "default").write_text("X")
    armors_mod.set_active_armor(state, "d")
    armors_mod.set_channels(state, "d", ["discord"])
    # Turn classified as telegram → discord-only armor stays dormant.
    assert rhema_mod.active_absolute_rule(state, channel_id="telegram") is None
    # Discord turn → fires.
    rule = rhema_mod.active_absolute_rule(state, channel_id="discord")
    assert rule is not None and rule.id == "abs-live-d"
    # No detection signal → channel-scoped armor stays dormant too.
    assert rhema_mod.active_absolute_rule(state, channel_id=None) is None
