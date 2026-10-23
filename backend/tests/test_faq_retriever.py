import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from app.knowledge.retriever import FAQRetriever


class FixedEmbeddingProvider:
    model_name = "fixed-test-model"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        vectors = []
        for text in texts:
            normalized = text.lower()
            if "invoice" in normalized or "receipt" in normalized:
                vectors.append([1.0, 0.0, 0.0])
            elif "password" in normalized or "login" in normalized:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return np.asarray(vectors, dtype=np.float32)


def write_faq(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "id": "invoice",
                    "category": "billing",
                    "question": "Where is my invoice?",
                    "answer": "Open Billing and download the invoice PDF.",
                    "aliases": ["receipt"],
                },
                {
                    "id": "password",
                    "category": "account",
                    "question": "How do I reset my password?",
                    "answer": "Use Forgot password on the sign-in page.",
                    "aliases": ["login"],
                },
            ]
        ),
        encoding="utf-8",
    )


def test_build_search_and_reload_faiss_index(tmp_path: Path) -> None:
    source = tmp_path / "faq.json"
    index_path = tmp_path / "index"
    write_faq(source)
    retriever = FAQRetriever(
        source_path=source,
        index_path=index_path,
        embedding_provider=FixedEmbeddingProvider(),
    )

    assert retriever.build() == 2
    match = retriever.search("I need an invoice receipt")

    assert match is not None
    assert match.entry_id == "invoice"
    assert match.category == "billing"
    assert match.confidence == pytest.approx(1.0)
    assert (index_path / "faq.faiss").exists()
    assert (index_path / "metadata.json").exists()

    reloaded = FAQRetriever(
        source_path=source,
        index_path=index_path,
        embedding_provider=FixedEmbeddingProvider(),
    )
    reloaded.load()
    assert reloaded.search("password login").entry_id == "password"


def test_search_respects_category_filter(tmp_path: Path) -> None:
    source = tmp_path / "faq.json"
    write_faq(source)
    retriever = FAQRetriever(
        source_path=source,
        index_path=tmp_path / "index",
        embedding_provider=FixedEmbeddingProvider(),
    )
    retriever.build()

    match = retriever.search("invoice receipt", category="account")

    assert match is not None
    assert match.entry_id == "password"
    assert match.category == "account"
    assert match.confidence == 0.0


def test_verbatim_curated_question_is_deterministically_grounded(
    tmp_path: Path,
) -> None:
    source = tmp_path / "faq.json"
    write_faq(source)
    retriever = FAQRetriever(
        source_path=source,
        index_path=tmp_path / "index",
        embedding_provider=FixedEmbeddingProvider(),
    )
    retriever.build()

    match = retriever.search(
        "Customer asks: HOW do I reset my password?! Please share the steps.",
        category="account",
    )

    assert match is not None
    assert match.entry_id == "password"
    assert match.confidence == 1.0


def test_exact_question_overrides_low_semantic_confidence(tmp_path: Path) -> None:
    class LowSimilarityEmbeddingProvider:
        model_name = "low-similarity-test-model"

        def embed(self, texts: Sequence[str]) -> np.ndarray:
            vectors = []
            for text in texts:
                if text.startswith("Customer asks:"):
                    vectors.append([0.6, 0.8])
                elif "reset my password" in text.lower():
                    vectors.append([1.0, 0.0])
                else:
                    vectors.append([0.0, 1.0])
            return np.asarray(vectors, dtype=np.float32)

    source = tmp_path / "faq.json"
    write_faq(source)
    retriever = FAQRetriever(
        source_path=source,
        index_path=tmp_path / "index",
        embedding_provider=LowSimilarityEmbeddingProvider(),
    )
    retriever.build()

    match = retriever.search(
        "Customer asks: How do I reset my password? Please share the steps.",
        category="account",
    )

    assert match is not None
    assert match.entry_id == "password"
    assert match.confidence == 1.0


def test_nonverbatim_query_keeps_semantic_confidence(tmp_path: Path) -> None:
    source = tmp_path / "faq.json"
    write_faq(source)
    retriever = FAQRetriever(
        source_path=source,
        index_path=tmp_path / "index",
        embedding_provider=FixedEmbeddingProvider(),
    )
    retriever.build()

    match = retriever.search("login help", category="account")

    assert match is not None
    assert match.entry_id == "password"
    assert match.confidence == pytest.approx(1.0)


def test_load_rejects_embedding_model_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "faq.json"
    index_path = tmp_path / "index"
    write_faq(source)
    retriever = FAQRetriever(
        source_path=source,
        index_path=index_path,
        embedding_provider=FixedEmbeddingProvider(),
    )
    retriever.build()
    metadata_path = index_path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["embedding_model"] = "different-model"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="embedding model"):
        retriever.load()


def test_zero_embeddings_are_rejected(tmp_path: Path) -> None:
    class ZeroEmbeddingProvider:
        model_name = "zero-model"

        def embed(self, texts: Sequence[str]) -> np.ndarray:
            return np.zeros((len(texts), 3), dtype=np.float32)

    source = tmp_path / "faq.json"
    write_faq(source)
    retriever = FAQRetriever(
        source_path=source,
        index_path=tmp_path / "index",
        embedding_provider=ZeroEmbeddingProvider(),
    )

    with pytest.raises(ValueError, match="zero vectors"):
        retriever.build()
