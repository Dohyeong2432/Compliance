"""Text embedding backends.

HashEmbedder is a dependency-free deterministic stand-in used to exercise
the retrieval pipeline end-to-end without any external API. VoyageEmbedder
wraps the real Voyage AI embeddings API for production use. Its request
building and response parsing are split into their own methods so they can
be unit tested against a mocked client without a live VOYAGE_API_KEY.
"""

from __future__ import annotations

import hashlib
import math
import os
from abc import ABC, abstractmethod
from typing import Sequence

Vector = list[float]


class Embedder(ABC):
    dimension: int

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[Vector]:
        ...

    def embed_one(self, text: str) -> Vector:
        return self.embed([text])[0]


class HashEmbedder(Embedder):
    """Deterministic character n-gram hashing embedder.

    This is NOT a semantic model — it has no notion of meaning or synonymy,
    only surface character overlap. It exists to exercise indexing,
    similarity search, and metadata filtering without an external API key.
    A prior evaluation found it can rank a sanctions case above the correct
    statute purely on lexical overlap, so treat search quality under this
    embedder as a pipeline smoke test, not a proxy for retrieval quality.
    Swap in VoyageEmbedder or GeminiEmbedder for real semantic recall;
    DEFAULT_MIN_SCORE in vector_store.py is calibrated to THIS embedder's
    score distribution and must be re-tuned when switching embedders.
    """

    def __init__(self, dimension: int = 256, ngram_sizes: tuple[int, ...] = (2, 3)):
        self.dimension = dimension
        self.ngram_sizes = ngram_sizes

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> Vector:
        vec = [0.0] * self.dimension
        normalized = text.lower()
        grams: list[str] = []
        for n in self.ngram_sizes:
            span = max(len(normalized) - n + 1, 0)
            grams.extend(normalized[i:i + n] for i in range(span))
        if not grams:
            grams = [normalized]
        for gram in grams:
            digest = int(hashlib.sha256(gram.encode("utf-8")).hexdigest(), 16)
            idx = digest % self.dimension
            sign = 1.0 if (digest // self.dimension) % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class VoyageEmbedder(Embedder):
    """Voyage AI embeddings API wrapper (voyage-3 family by default)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "voyage-3",
        dimension: int = 1024,
        input_type: str = "document",
    ):
        self.api_key = api_key or os.environ.get("VOYAGE_API_KEY")
        if not self.api_key:
            raise RuntimeError("VOYAGE_API_KEY is not set; cannot construct VoyageEmbedder")
        self.model = model
        self.dimension = dimension
        self.input_type = input_type
        self._client = self._build_client()

    def _build_client(self):
        import voyageai  # optional dependency, only needed for live calls

        return voyageai.Client(api_key=self.api_key)

    def _build_request(self, texts: Sequence[str]) -> dict:
        return {"texts": list(texts), "model": self.model, "input_type": self.input_type}

    def _parse_response(self, response) -> list[Vector]:
        embeddings = getattr(response, "embeddings", None)
        if embeddings is None and isinstance(response, dict):
            embeddings = response.get("embeddings")
        if embeddings is None:
            raise ValueError("Voyage response is missing 'embeddings'")
        return [list(e) for e in embeddings]

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        request = self._build_request(texts)
        response = self._client.embed(**request)
        return self._parse_response(response)


class GeminiEmbedder(Embedder):
    """Gemini embeddings API wrapper (gemini-embedding-001 by default).

    gemini-embedding-001 natively outputs 3072-d vectors but supports
    Matryoshka truncation via output_dimensionality, so `dimension` both
    requests and validates the size actually returned. Same DEFAULT_MIN_SCORE
    recalibration caveat as VoyageEmbedder applies when switching to this
    embedder (see HashEmbedder docstring).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-embedding-001",
        dimension: int = 768,
        task_type: str = "RETRIEVAL_DOCUMENT",
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set; cannot construct GeminiEmbedder")
        self.model = model
        self.dimension = dimension
        self.task_type = task_type
        self._client = self._build_client()

    def _build_client(self):
        from google import genai  # optional dependency, only needed for live calls

        return genai.Client(api_key=self.api_key)

    def _build_request(self, texts: Sequence[str]) -> dict:
        return {
            "model": self.model,
            "contents": list(texts),
            "config": {"task_type": self.task_type, "output_dimensionality": self.dimension},
        }

    def _parse_response(self, response) -> list[Vector]:
        embeddings = getattr(response, "embeddings", None)
        if embeddings is None and isinstance(response, dict):
            embeddings = response.get("embeddings")
        if embeddings is None:
            raise ValueError("Gemini response is missing 'embeddings'")
        return [self._values_of(e) for e in embeddings]

    def _values_of(self, embedding) -> Vector:
        if isinstance(embedding, dict):
            return list(embedding["values"])
        return list(embedding.values)

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        request = self._build_request(texts)
        response = self._client.models.embed_content(**request)
        return self._parse_response(response)
