from app.config import get_settings
from app.knowledge.retriever import FAQRetriever, HuggingFaceEmbeddingProvider


def main() -> None:
    settings = get_settings()
    retriever = FAQRetriever(
        source_path=settings.faq_source_path,
        index_path=settings.faq_index_path,
        embedding_provider=HuggingFaceEmbeddingProvider(settings.embedding_model),
    )
    count = retriever.build()
    print(f"Built FAQ index with {count} entries at {settings.faq_index_path}")


if __name__ == "__main__":
    main()
