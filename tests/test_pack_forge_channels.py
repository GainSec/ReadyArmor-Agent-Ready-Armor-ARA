from pathlib import Path

from ara.forge.phases.exhaust import _EXTRACT_CHANNELS
from ara.forge import channels as fc

_NEW = ("propose.alphabet", "propose.binding", "propose.chunk", "propose.detection")


def test_propose_channels_in_exhaust_allowlist() -> None:
    for c in _NEW:
        assert c in _EXTRACT_CHANNELS


def test_propose_channels_registered(tmp_path: Path) -> None:
    installed = fc.install_forge_channels(tmp_path)
    for c in _NEW:
        assert c in installed
        assert installed[c].name == c
