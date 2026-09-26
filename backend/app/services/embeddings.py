"""Local Hugging Face sentence-transformers embeddings for RAG."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import structlog

from backend.app.config import Settings, get_settings

logger = structlog.get_logger(__name__)


class EmbeddingError(RuntimeError):
    """Raised when the embedding model cannot be loaded or run."""


@lru_cache
def _load_model(model_name: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise EmbeddingError(
            "sentence-transformers is not installed. "
            "Run: uv sync --extra rag"
        ) from exc
    logger.info("rag_embedding_model_loading", model=model_name)
    return SentenceTransformer(model_name)


def get_embedding_model(settings: Settings | None = None) -> Any:
    cfg = settings or get_settings()
    return _load_model(cfg.rag_embedding_model)


def embed_texts(
    texts: list[str],
    *,
    settings: Settings | None = None,
) -> list[list[float]]:
    """
    Encode texts with the configured Hugging Face model.

    Returns L2-normalized vectors suitable for cosine distance in pgvector.
    """
    if not texts:
        return []
    cfg = settings or get_settings()
    model = get_embedding_model(cfg)
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    rows = [list(map(float, row)) for row in vectors]
    dims = len(rows[0]) if rows else 0
    if dims != cfg.rag_embedding_dims:
        raise EmbeddingError(
            f"Embedding dim {dims} != configured RAG_EMBEDDING_DIMS "
            f"{cfg.rag_embedding_dims} (model={cfg.rag_embedding_model}). "
            "Update env + pgvector column together before changing models."
        )
    return rows


def embed_query(query: str, *, settings: Settings | None = None) -> list[float]:
    vectors = embed_texts([query], settings=settings)
    return vectors[0]
