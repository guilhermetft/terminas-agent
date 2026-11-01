"""Geradores de embeddings 100% locais (CPU, sem APIs externas de embeddings)."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_MODEL_CACHE = Path(".terminas") / "models"


@runtime_checkable
class Embedder(Protocol):
    """Contrato de um gerador de embeddings. `name` identifica o espaço vetorial."""

    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [float(x) for x in vector] if norm == 0 else [x / norm for x in vector]


def _fold(text: str) -> str:
    """minúsculas e sem acentos: 'Porta Padrão' -> 'porta padrao'."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


class HashingEmbedder:
    """Embedder determinístico e leve: bag-of-words + trigramas de caracteres via hashing.

    Sem dependências nem download de modelo. Captura similaridade lexical (não semântica);
    serve de fallback offline e de dublê determinístico nos testes.
    """

    def __init__(self, dim: int = 384) -> None:
        if dim < 8:
            raise ValueError("dim deve ser >= 8.")
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _features(self, text: str) -> list[str]:
        features: list[str] = []
        for word in re.findall(r"\w+", _fold(text)):
            features.append(f"w:{word}")
            padded = f"<{word}>"
            features.extend(f"c:{padded[i : i + 3]}" for i in range(len(padded) - 2))
        return features

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feature in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            sign = 1.0 if value & 1 else -1.0
            vec[(value >> 1) % self.dim] += sign
        return normalize(vec)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class FastEmbedEmbedder:
    """Embeddings semânticos locais via FastEmbed (ONNX em CPU).

    O modelo é baixado uma única vez para `cache_dir`; depois roda offline.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_FASTEMBED_MODEL,
        cache_dir: str | Path | None = DEFAULT_MODEL_CACHE,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = None if cache_dir is None else str(cache_dir)
        self.name = f"fastembed:{model_name}"
        self._model = None

    def _load(self):  # noqa: ANN202 - tipo vem de import tardio
        if self._model is None:
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise ImportError(
                    "FastEmbed não instalado. Instale com: pip install -e \".[rag]\""
                ) from exc
            self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return [normalize(vec.tolist()) for vec in self._load().embed(list(texts))]


def default_embedder() -> Embedder:
    """FastEmbed se instalado; senão `HashingEmbedder` (com aviso de qualidade reduzida)."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        logger.warning(
            "FastEmbed ausente: usando HashingEmbedder (apenas similaridade lexical). "
            'Para busca semântica: pip install -e ".[rag]"'
        )
        return HashingEmbedder()
    return FastEmbedEmbedder()
