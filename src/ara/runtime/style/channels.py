"""StyleScoreChannel — gem-restricted to the writeup chunk."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ara.runtime.channels import registry
from ara.runtime.channels.base import Channel, ChannelContext, ChannelRejection
from ara.runtime.style.profile import WeaponStyle, read as read_style
from ara.runtime.style.scorer import score_section


@dataclass
class StyleScoreBody:
    text: str
    section_name: str
    chunk_id: str | None = None  # for the gem-restriction check
    iteration: int = 1


class StyleScoreChannel(Channel):
    name = "style.score"
    schema = StyleScoreBody
    sink_subsystem = "style_telemetry.jsonl"

    def __init__(
        self,
        *,
        weapon_dir: Path | None = None,
        weapon_style: WeaponStyle | None = None,
        allowed_chunks: tuple[str, ...] = ("writeup",),
    ) -> None:
        if weapon_style is None and weapon_dir is not None:
            self.weapon_style = read_style(Path(weapon_dir) / "style" / "style.json")
        else:
            self.weapon_style = weapon_style or WeaponStyle()
        self.allowed_chunks = allowed_chunks

    def execute(self, body: dict, ctx: ChannelContext) -> dict:
        if not (body.get("text") or "").strip():
            raise ChannelRejection("text is empty")
        chunk = body.get("chunk_id")
        if chunk is not None and chunk not in self.allowed_chunks:
            raise ChannelRejection(
                f"style.score restricted to {self.allowed_chunks}; got chunk={chunk!r}"
            )
        score = score_section(
            body["text"],
            section_name=body["section_name"],
            weapon_style=self.weapon_style,
        )
        return {"score": score.to_jsonable()}


registry.register_class(StyleScoreChannel)


def install_style_channel(
    *, weapon_dir: Path | None = None,
    weapon_style: WeaponStyle | None = None,
    allowed_chunks: tuple[str, ...] = ("writeup",),
) -> Channel:
    """Idempotent: returns the already-registered instance if present."""
    if registry.has("style.score"):
        return registry.get("style.score")
    ch = StyleScoreChannel(
        weapon_dir=weapon_dir,
        weapon_style=weapon_style,
        allowed_chunks=allowed_chunks,
    )
    registry.register(ch)
    return ch


# Silence unused-import warning
_ = field
