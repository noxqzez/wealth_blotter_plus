"""Filtered semantic retrieval over document_chunks via pgvector RPC."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from backend.app.config import get_settings
from backend.app.core.supabase import get_service_supabase
from backend.app.schemas.payload_models import RagChunkHit, RagRetrieveResponse
from backend.app.services.embeddings import embed_query

logger = structlog.get_logger(__name__)


class RagRetrieveError(RuntimeError):
    """Raised when RAG retrieval fails."""


async def retrieve_research_chunks(
    *,
    advisor_id: UUID,
    query: str,
    ticker: str | None = None,
    asset_class: str | None = None,
    top_k: int = 5,
) -> RagRetrieveResponse:
    """Embed the query and call match_document_chunks with advisor/ticker filters."""
    settings = get_settings()
    query_vector = embed_query(query.strip(), settings=settings)
    client = get_service_supabase()

    from backend.app.services.ticker_normalize import normalize_ticker

    filter_ticker = normalize_ticker(ticker) if ticker else None
    params: dict[str, Any] = {
        "query_embedding": query_vector,
        "match_count": top_k,
        "filter_advisor_id": str(advisor_id),
        "filter_ticker": filter_ticker or None,
        "filter_asset_class": asset_class.strip() if asset_class else None,
    }

    try:
        result = client.rpc("match_document_chunks", params).execute()
    except Exception as exc:  # noqa: BLE001
        raise RagRetrieveError(f"match_document_chunks RPC failed: {exc}") from exc

    hits: list[RagChunkHit] = []
    for row in result.data or []:
        hits.append(
            RagChunkHit(
                chunk_id=UUID(str(row["chunk_id"])),
                document_id=UUID(str(row["document_id"])),
                content=str(row["content"]),
                chunk_index=int(row["chunk_index"]),
                page_number=row.get("page_number"),
                ticker=row.get("ticker"),
                asset_class=row.get("asset_class"),
                storage_path=row.get("storage_path"),
                original_filename=row.get("original_filename"),
                similarity=float(row.get("similarity") or 0.0),
            )
        )

    logger.info(
        "rag_retrieve",
        advisor_id=str(advisor_id),
        hit_count=len(hits),
        ticker=ticker,
        asset_class=asset_class,
    )
    return RagRetrieveResponse(
        advisor_id=advisor_id,
        query=query,
        hits=hits,
    )
