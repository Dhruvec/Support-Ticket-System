import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import faiss
import numpy as np
from pydantic import BaseModel, Field, TypeAdapter


class FAQEntry(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)

    @property
    def search_text(self) -> str:
        aliases = "; ".join(self.aliases)
        return f"Category: {self.category}\nQuestion: {self.question}\nAliases: {aliases}\nAnswer: {self.answer}"


@dataclass(frozen=True)
class FAQMatch:
    entry_id: str
    category: str
    question: str
    answer: str
    confidence: float


class FAQSearchService(Protocol):
    def search(self, query: str, *, category: str | None = None) -> FAQMatch | None: ...


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


class HuggingFaceEmbeddingProvider:
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        vectors = self._model.encode(
            list(texts),
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


class FAQRetriever:
    INDEX_FILENAME = "faq.faiss"
    METADATA_FILENAME = "metadata.json"

    def __init__(
        self,
        *,
        source_path: str | Path,
        index_path: str | Path,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.source_path = Path(source_path)
        self.index_path = Path(index_path)
        self.embedding_provider = embedding_provider
        self._index: faiss.Index | None = None
        self._entries: list[FAQEntry] = []

    def build(self) -> int:
        entries = self._load_source()
        vectors = self._normalized(self.embedding_provider.embed([entry.search_text for entry in entries]))
        if vectors.shape[0] != len(entries):
            raise ValueError("Embedding provider returned an unexpected number of vectors")

        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        self._persist(index, entries)
        self._index = index
        self._entries = entries
        return len(entries)

    def load(self) -> None:
        metadata_path = self.index_path / self.METADATA_FILENAME
        index_file = self.index_path / self.INDEX_FILENAME
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata["embedding_model"] != self.embedding_provider.model_name:
            raise ValueError("FAQ index embedding model does not match configured model")
        entries = TypeAdapter(list[FAQEntry]).validate_python(metadata["entries"])
        index = faiss.read_index(str(index_file))
        if index.ntotal != len(entries):
            raise ValueError("FAQ index and metadata entry counts do not match")
        self._index = index
        self._entries = entries

    def ensure_loaded(self) -> None:
        if self._index is not None:
            return
        try:
            self.load()
        except FileNotFoundError:
            self.build()

    def search(self, query: str, *, category: str | None = None) -> FAQMatch | None:
        if not query.strip():
            return None
        self.ensure_loaded()
        assert self._index is not None

        for entry in self._entries:
            if category is not None and entry.category != category:
                continue
            if self._exact_question_confidence(query, entry) == 1.0:
                return FAQMatch(
                    entry_id=entry.id,
                    category=entry.category,
                    question=entry.question,
                    answer=entry.answer,
                    confidence=1.0,
                )

        query_vector = self._normalized(self.embedding_provider.embed([query]))
        limit = len(self._entries)
        scores, indices = self._index.search(query_vector, limit)
        for score, position in zip(scores[0], indices[0], strict=True):
            if position < 0:
                continue
            entry = self._entries[int(position)]
            if category is not None and entry.category != category:
                continue
            return FAQMatch(
                entry_id=entry.id,
                category=entry.category,
                question=entry.question,
                answer=entry.answer,
                confidence=max(0.0, min(1.0, float(score))),
            )
        return None

    @staticmethod
    def _exact_question_confidence(query: str, entry: FAQEntry) -> float:
        """Treat a verbatim curated FAQ question as deterministic grounding."""
        normalized_query = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
        normalized_question = " ".join(
            re.findall(r"[a-z0-9]+", entry.question.lower())
        )
        if normalized_question and normalized_question in normalized_query:
            return 1.0
        return 0.0

    def _load_source(self) -> list[FAQEntry]:
        raw = json.loads(self.source_path.read_text(encoding="utf-8"))
        entries = TypeAdapter(list[FAQEntry]).validate_python(raw)
        if not entries:
            raise ValueError("FAQ source must contain at least one entry")
        ids = [entry.id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("FAQ entry IDs must be unique")
        return entries

    def _persist(self, index: faiss.Index, entries: list[FAQEntry]) -> None:
        self.index_path.mkdir(parents=True, exist_ok=True)
        index_tmp = self.index_path / f"{self.INDEX_FILENAME}.tmp"
        metadata_tmp = self.index_path / f"{self.METADATA_FILENAME}.tmp"
        faiss.write_index(index, str(index_tmp))
        metadata_tmp.write_text(
            json.dumps(
                {
                    "embedding_model": self.embedding_provider.model_name,
                    "entries": [entry.model_dump() for entry in entries],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(index_tmp, self.index_path / self.INDEX_FILENAME)
        os.replace(metadata_tmp, self.index_path / self.METADATA_FILENAME)

    @staticmethod
    def _normalized(vectors: np.ndarray) -> np.ndarray:
        normalized = np.asarray(vectors, dtype=np.float32)
        if normalized.ndim != 2 or normalized.shape[0] == 0 or normalized.shape[1] == 0:
            raise ValueError("Embeddings must be a non-empty two-dimensional matrix")
        normalized = normalized.copy()
        faiss.normalize_L2(normalized)
        if np.any(np.linalg.norm(normalized, axis=1) == 0):
            raise ValueError("Embeddings must not contain zero vectors")
        return normalized
