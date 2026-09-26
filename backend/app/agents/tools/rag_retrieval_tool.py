"""Deterministic RAG retrieval tool for investment analysis agents."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from backend.app.services.rag_retrieve import retrieve_research_chunks


async def lookup_research_context(
    *,
    advisor_id: UUID,
    query: str,
    ticker: str | None = None,
    asset_class: str | None = None,
    top_k: int = 5,
) -> str:
    """
    Return JSON string of filtered research chunks for agent tool use.

    Prefer passing ticker and/or asset_class to avoid cross-asset contamination.
    """
    result = await retrieve_research_chunks(
        advisor_id=advisor_id,
        query=query,
        ticker=ticker,
        asset_class=asset_class,
        top_k=top_k,
    )
    payload: dict[str, Any] = {
        "query": result.query,
        "hit_count": len(result.hits),
        "hits": [
            {
                "content": h.content,
                "ticker": h.ticker,
                "asset_class": h.asset_class,
                "similarity": h.similarity,
                "page_number": h.page_number,
                "original_filename": h.original_filename,
                "storage_path": h.storage_path,
                "chunk_index": h.chunk_index,
            }
            for h in result.hits
        ],
    }
    return json.dumps(payload, default=str)
