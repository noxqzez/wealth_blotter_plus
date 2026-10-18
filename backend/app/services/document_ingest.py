"""Research PDF upload → Storage → classify → embed → pgvector."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog

from backend.app.agents.doc_classify import classify_document_excerpt
from backend.app.config import Settings, get_settings
from backend.app.core.supabase import get_service_supabase, service_table
from backend.app.schemas.agent_outputs import ResearchAssetClass
from backend.app.schemas.payload_models import (
    DocumentIngestJobResponse,
    DocumentIngestJobStatus,
    DocumentUploadResponse,
)
from backend.app.services.embeddings import embed_texts
from backend.app.services.pdf_extract import chunk_pages, extract_pdf_bytes

logger = structlog.get_logger(__name__)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class DocumentIngestError(RuntimeError):
    """Domain error for document ingest failures."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sanitize_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = _SAFE_FILENAME.sub("_", base).strip("._")
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf" if cleaned else "document.pdf"
    return cleaned[:180]


def _storage_object_path(*, advisor_id: UUID, job_id: UUID, filename: str) -> str:
    return f"{advisor_id}/{job_id}/{filename}"


async def upload_research_pdf(
    *,
    advisor_id: UUID,
    filename: str,
    content: bytes,
    content_type: str | None = None,
    auto_process: bool | None = None,
) -> DocumentUploadResponse:
    """Upload PDF to Supabase Storage and enqueue an ingest job."""
    from backend.app.services.document_upload_guards import enforce_rag_upload_guards

    settings = get_settings()
    if not content:
        raise DocumentIngestError("Empty file")
    if len(content) > settings.rag_max_upload_bytes:
        raise DocumentIngestError(
            f"File exceeds RAG_MAX_UPLOAD_BYTES ({settings.rag_max_upload_bytes})"
        )
    if not filename.lower().endswith(".pdf"):
        raise DocumentIngestError("Only PDF uploads are supported in this POC")

    page_count = enforce_rag_upload_guards(
        advisor_id=advisor_id,
        content=content,
        settings=settings,
    )

    safe_name = _sanitize_filename(filename)
    job_id = uuid4()
    bucket = settings.rag_storage_bucket
    storage_path = _storage_object_path(
        advisor_id=advisor_id, job_id=job_id, filename=safe_name
    )

    client = get_service_supabase()
    file_options: dict[str, str] = {
        "content-type": content_type or "application/pdf",
        "upsert": "false",
    }

    def _upload() -> None:
        result = client.storage.from_(bucket).upload(
            path=storage_path,
            file=content,
            file_options=file_options,
        )
        # supabase-py may return dict-like error payloads depending on version
        if isinstance(result, dict) and result.get("error"):
            raise DocumentIngestError(str(result["error"]))

    try:
        await asyncio.to_thread(_upload)
    except DocumentIngestError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentIngestError(
            f"Storage upload failed (create private bucket '{bucket}'?): {exc}"
        ) from exc

    row: dict[str, Any] = {
        "id": str(job_id),
        "advisor_id": str(advisor_id),
        "storage_bucket": bucket,
        "storage_path": storage_path,
        "original_filename": safe_name,
        "status": DocumentIngestJobStatus.QUEUED.value,
        "page_count": int(page_count),
        "updated_at": _utc_now_iso(),
    }
    try:
        insert_res = service_table("document_ingest_jobs").insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — pre-migration DBs lack page_count
        logger.warning(
            "document_ingest_insert_with_page_count_failed",
            error=str(exc),
            hint="Apply migration_rag_upload_guards.sql",
        )
        row.pop("page_count", None)
        insert_res = service_table("document_ingest_jobs").insert(row).execute()
    if not insert_res.data:
        raise DocumentIngestError("Failed to enqueue ingest job")

    should_auto = (
        settings.rag_auto_process_upload if auto_process is None else auto_process
    )
    logger.info(
        "document_uploaded",
        job_id=str(job_id),
        advisor_id=str(advisor_id),
        storage_path=storage_path,
        page_count=page_count,
        auto_process=should_auto,
    )
    return DocumentUploadResponse(
        job_id=job_id,
        advisor_id=advisor_id,
        storage_bucket=bucket,
        storage_path=storage_path,
        original_filename=safe_name,
        status=DocumentIngestJobStatus.QUEUED,
        auto_process=should_auto,
        page_count=page_count,
    )


def _row_to_job_response(
    row: dict[str, Any],
    *,
    asset_class: str | None = None,
    ticker: str | None = None,
    chunk_count: int | None = None,
) -> DocumentIngestJobResponse:
    return DocumentIngestJobResponse(
        job_id=UUID(str(row["id"])),
        advisor_id=UUID(str(row["advisor_id"])),
        storage_bucket=str(row["storage_bucket"]),
        storage_path=str(row["storage_path"]),
        original_filename=str(row["original_filename"]),
        status=DocumentIngestJobStatus(str(row["status"])),
        error_message=row.get("error_message"),
        document_id=UUID(str(row["document_id"])) if row.get("document_id") else None,
        asset_class=asset_class,
        ticker=ticker,
        chunk_count=chunk_count,
    )


async def get_ingest_job(
    job_id: UUID, *, advisor_id: UUID
) -> DocumentIngestJobResponse:
    res = (
        service_table("document_ingest_jobs")
        .select("*")
        .eq("id", str(job_id))
        .eq("advisor_id", str(advisor_id))
        .limit(1)
        .execute()
    )
    if not res.data:
        raise DocumentIngestError("Job not found")
    row = res.data[0]
    asset_class: str | None = None
    ticker: str | None = None
    chunk_count: int | None = None
    if row.get("document_id"):
        doc_res = (
            service_table("document_registry")
            .select("asset_class,ticker")
            .eq("id", str(row["document_id"]))
            .limit(1)
            .execute()
        )
        if doc_res.data:
            asset_class = doc_res.data[0].get("asset_class")
            ticker = doc_res.data[0].get("ticker")
        chunks_res = (
            service_table("document_chunks")
            .select("id", count="exact")
            .eq("document_id", str(row["document_id"]))
            .execute()
        )
        chunk_count = chunks_res.count if chunks_res.count is not None else None
    return _row_to_job_response(
        row, asset_class=asset_class, ticker=ticker, chunk_count=chunk_count
    )


async def process_ingest_job(
    job_id: UUID,
    *,
    advisor_id: UUID | None = None,
    settings: Settings | None = None,
) -> DocumentIngestJobResponse:
    """
    Run classify → chunk → embed for a queued (or retriable failed) job.

    Quarantines Neither classifications without writing chunks.
    """
    cfg = settings or get_settings()
    res = (
        service_table("document_ingest_jobs")
        .select("*")
        .eq("id", str(job_id))
        .limit(1)
        .execute()
    )
    if not res.data:
        raise DocumentIngestError("Job not found")
    job = res.data[0]
    if advisor_id is not None and str(job["advisor_id"]) != str(advisor_id):
        raise DocumentIngestError("Job not found")

    status = str(job["status"])
    if status == DocumentIngestJobStatus.READY.value:
        return await get_ingest_job(job_id, advisor_id=UUID(str(job["advisor_id"])))
    if status == DocumentIngestJobStatus.PROCESSING.value:
        raise DocumentIngestError("Job already processing")

    service_table("document_ingest_jobs").update(
        {
            "status": DocumentIngestJobStatus.PROCESSING.value,
            "started_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "error_message": None,
        }
    ).eq("id", str(job_id)).execute()

    try:
        return await _run_pipeline(job, cfg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("document_ingest_failed", job_id=str(job_id))
        service_table("document_ingest_jobs").update(
            {
                "status": DocumentIngestJobStatus.FAILED.value,
                "error_message": str(exc)[:2000],
                "updated_at": _utc_now_iso(),
                "completed_at": _utc_now_iso(),
            }
        ).eq("id", str(job_id)).execute()
        raise DocumentIngestError(str(exc)) from exc


async def _run_pipeline(job: dict[str, Any], cfg: Settings) -> DocumentIngestJobResponse:
    job_id = UUID(str(job["id"]))
    advisor_id = UUID(str(job["advisor_id"]))
    bucket = str(job["storage_bucket"])
    storage_path = str(job["storage_path"])
    client = get_service_supabase()

    def _download() -> bytes:
        return client.storage.from_(bucket).download(storage_path)

    pdf_bytes = await asyncio.to_thread(_download)
    extracted = await asyncio.to_thread(extract_pdf_bytes, pdf_bytes)
    excerpt = extracted.full_text[: cfg.rag_classify_excerpt_chars]
    if not excerpt.strip():
        raise DocumentIngestError(
            "No extractable text (scanned PDFs need OCR — not in POC)"
        )

    classification = await classify_document_excerpt(excerpt)
    is_neither = classification.asset_class == ResearchAssetClass.NEITHER
    registry_status = "quarantined" if is_neither else "indexed"

    # Replace prior registry row for same storage path (re-process)
    existing = (
        service_table("document_registry")
        .select("id")
        .eq("storage_bucket", bucket)
        .eq("storage_path", storage_path)
        .limit(1)
        .execute()
    )
    if existing.data:
        old_id = existing.data[0]["id"]
        service_table("document_chunks").delete().eq("document_id", old_id).execute()
        service_table("document_registry").delete().eq("id", old_id).execute()

    ticker = classification.ticker.strip().upper() if classification.ticker else None
    registry_row = {
        "advisor_id": str(advisor_id),
        "ingest_job_id": str(job_id),
        "storage_bucket": bucket,
        "storage_path": storage_path,
        "original_filename": job["original_filename"],
        "asset_class": classification.asset_class.value,
        "ticker": ticker,
        "source_firm": classification.source_firm,
        "overall_sentiment": classification.overall_sentiment.value,
        "classification_confidence": classification.confidence_score,
        "status": registry_status,
        "embedding_model": None if is_neither else cfg.rag_embedding_model,
        "processed_at": _utc_now_iso(),
    }
    reg_res = service_table("document_registry").insert(registry_row).execute()
    if not reg_res.data:
        raise DocumentIngestError("Failed to write document_registry")
    document_id = UUID(str(reg_res.data[0]["id"]))

    chunk_count = 0
    if not is_neither:
        chunks = chunk_pages(
            extracted.pages,
            chunk_size=cfg.rag_chunk_size,
            chunk_overlap=cfg.rag_chunk_overlap,
        )
        if not chunks:
            raise DocumentIngestError("No chunks produced from PDF text")

        texts = [c.content for c in chunks]
        embeddings = await asyncio.to_thread(embed_texts, texts, settings=cfg)

        # Batch insert in small groups to avoid payload limits
        batch_size = 50
        for start in range(0, len(chunks), batch_size):
            batch = []
            for chunk, vector in zip(
                chunks[start : start + batch_size],
                embeddings[start : start + batch_size],
                strict=True,
            ):
                batch.append(
                    {
                        "document_id": str(document_id),
                        "advisor_id": str(advisor_id),
                        "chunk_index": chunk.chunk_index,
                        "page_number": chunk.page_number,
                        "content": chunk.content,
                        "embedding": vector,
                    }
                )
            service_table("document_chunks").insert(batch).execute()
        chunk_count = len(chunks)

    final_status = (
        DocumentIngestJobStatus.QUARANTINED
        if is_neither
        else DocumentIngestJobStatus.READY
    )
    service_table("document_ingest_jobs").update(
        {
            "status": final_status.value,
            "document_id": str(document_id),
            "updated_at": _utc_now_iso(),
            "completed_at": _utc_now_iso(),
            "error_message": None,
        }
    ).eq("id", str(job_id)).execute()

    logger.info(
        "document_ingest_complete",
        job_id=str(job_id),
        document_id=str(document_id),
        status=final_status.value,
        asset_class=classification.asset_class.value,
        chunk_count=chunk_count,
    )
    return DocumentIngestJobResponse(
        job_id=job_id,
        advisor_id=advisor_id,
        storage_bucket=bucket,
        storage_path=storage_path,
        original_filename=str(job["original_filename"]),
        status=final_status,
        document_id=document_id,
        asset_class=classification.asset_class.value,
        ticker=ticker,
        chunk_count=chunk_count if not is_neither else 0,
    )


async def process_next_queued_job(
    *,
    settings: Settings | None = None,
) -> DocumentIngestJobResponse | None:
    """Claim the oldest queued job (worker loop helper)."""
    res = (
        service_table("document_ingest_jobs")
        .select("*")
        .eq("status", DocumentIngestJobStatus.QUEUED.value)
        .order("created_at")
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    job_id = UUID(str(res.data[0]["id"]))
    return await process_ingest_job(job_id, settings=settings)
