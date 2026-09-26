"""Research document upload, ingest status, and RAG retrieve (POC)."""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)

from backend.app.core.security import AuthContext, get_current_user
from backend.app.schemas.payload_models import (
    DocumentIngestJobResponse,
    DocumentUploadResponse,
    RagRetrieveRequest,
    RagRetrieveResponse,
)
from backend.app.services.document_ingest import (
    DocumentIngestError,
    get_ingest_job,
    process_ingest_job,
    upload_research_pdf,
)
from backend.app.services.embeddings import EmbeddingError
from backend.app.services.rag_retrieve import RagRetrieveError, retrieve_research_chunks

router = APIRouter()
logger = structlog.get_logger(__name__)


async def _background_process(job_id: str, advisor_id: str) -> None:
    try:
        await process_ingest_job(UUID(job_id), advisor_id=UUID(advisor_id))
    except Exception as exc:  # noqa: BLE001 — background must not raise to caller
        logger.warning(
            "document_auto_process_failed",
            job_id=job_id,
            error=str(exc),
        )


@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    user: AuthContext = Depends(get_current_user),
    file: UploadFile = File(...),
) -> DocumentUploadResponse:
    """
    Upload a research PDF to Supabase Storage and enqueue classification + embedding.

    When RAG_AUTO_PROCESS_UPLOAD=true, processing runs in a FastAPI background task.
    """
    raw = await file.read()
    try:
        result = await upload_research_pdf(
            advisor_id=user.advisor_id,
            filename=file.filename or "document.pdf",
            content=raw,
            content_type=file.content_type,
        )
    except DocumentIngestError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    if result.auto_process:
        background_tasks.add_task(
            _background_process, str(result.job_id), str(user.advisor_id)
        )
    return result


@router.get("/jobs/{job_id}", response_model=DocumentIngestJobResponse)
async def get_document_job(
    job_id: UUID,
    user: AuthContext = Depends(get_current_user),
) -> DocumentIngestJobResponse:
    try:
        return await get_ingest_job(job_id, advisor_id=user.advisor_id)
    except DocumentIngestError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.post("/jobs/{job_id}/process", response_model=DocumentIngestJobResponse)
async def process_document_job(
    job_id: UUID,
    user: AuthContext = Depends(get_current_user),
) -> DocumentIngestJobResponse:
    """Synchronously run classify → chunk → embed for a queued/failed job."""
    try:
        return await process_ingest_job(job_id, advisor_id=user.advisor_id)
    except EmbeddingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except DocumentIngestError as exc:
        detail = str(exc)
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in detail.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=detail) from exc


@router.post("/retrieve", response_model=RagRetrieveResponse)
async def retrieve_documents(
    payload: RagRetrieveRequest,
    user: AuthContext = Depends(get_current_user),
) -> RagRetrieveResponse:
    """Semantic search over indexed research chunks (optional ticker/asset filters)."""
    try:
        return await retrieve_research_chunks(
            advisor_id=user.advisor_id,
            query=payload.query,
            ticker=payload.ticker,
            asset_class=payload.asset_class,
            top_k=payload.top_k,
        )
    except EmbeddingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except RagRetrieveError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
