"""EmbeddingProvider protocol + implementations.

v1 ships with :class:`NullEmbedder` (default — search runs FTS-only) and
the :class:`LocalOnnxEmbedder` shell. The ONNX shell lazily imports its
heavy dependencies; if onnxruntime/tokenizers aren't installed,
``LocalOnnxEmbedder`` raises a ``RuntimeError`` at construction. This
keeps v1 importable without ML deps.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingPin:
    provider: str
    model_id: str | None
    weights_sha256: str | None
    dim: int | None
    pinned_at: float


class EmbeddingProvider(Protocol):
    name: str
    dim: int | None

    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def pin_descriptor(self) -> dict: ...
    def verify_pin(self, descriptor: dict) -> bool: ...


class NullEmbedder:
    name = "null"
    dim: int | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        return []

    def pin_descriptor(self) -> dict:
        return {
            "provider": "null", "model_id": None,
            "weights_sha256": None, "dim": None,
            "pinned_at": time.time(),
        }

    def verify_pin(self, descriptor: dict) -> bool:
        return descriptor.get("provider") == "null"


class LocalOnnxEmbedder:
    """Shell. Lazy-imports onnxruntime + tokenizers on construction."""
    name = "local-onnx"

    def __init__(
        self,
        model_id: str = "bge-small-en-v1.5",
        cache_dir: Path | None = None,
        max_batch: int = 32,
    ) -> None:
        try:
            import onnxruntime  # type: ignore[import-not-found]  # noqa: F401
            import tokenizers  # type: ignore[import-not-found]  # noqa: F401
        except Exception as e:
            raise RuntimeError(
                "LocalOnnxEmbedder requires onnxruntime + tokenizers; "
                f"install via pip if you want to enable embeddings: {e}"
            ) from e
        self.model_id = model_id
        self.cache_dir = cache_dir or Path.home() / ".ara" / "embedder_models"
        self.max_batch = max_batch
        self.dim: int | None = 384  # bge-small dim
        self._weights_sha: str | None = None  # populated on first embed

    def embed(self, texts: list[str]) -> list[list[float]]:
        # v1 placeholder: weights not yet bundled / autodownloaded.
        # Returns deterministic zero-vectors so the dispatcher path is
        # exercisable in tests without real ONNX weights.
        return [[0.0] * (self.dim or 384) for _ in texts]

    def pin_descriptor(self) -> dict:
        return {
            "provider": "local-onnx",
            "model_id": self.model_id,
            "weights_sha256": self._weights_sha,
            "dim": self.dim,
            "pinned_at": time.time(),
        }

    def verify_pin(self, descriptor: dict) -> bool:
        return (
            descriptor.get("provider") == "local-onnx"
            and descriptor.get("model_id") == self.model_id
        )


def make_embedder(provider: str, **kwargs) -> EmbeddingProvider:
    if provider == "null":
        return NullEmbedder()
    if provider == "local-onnx":
        return LocalOnnxEmbedder(**kwargs)
    raise ValueError(f"unknown embedder provider: {provider!r}")
