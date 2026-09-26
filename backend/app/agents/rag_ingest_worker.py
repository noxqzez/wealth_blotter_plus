"""Poll queued document ingest jobs (optional alternative to upload auto-process)."""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from backend.app.services.document_ingest import (
    DocumentIngestError,
    process_next_queued_job,
)

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


async def _loop(*, once: bool, poll_seconds: float) -> None:
    logger.info("rag_ingest_worker_start", once=once, poll_seconds=poll_seconds)
    while True:
        try:
            result = await process_next_queued_job()
            if result is None:
                if once:
                    logger.info("rag_ingest_worker_idle_exit")
                    return
                await asyncio.sleep(poll_seconds)
                continue
            logger.info(
                "rag_ingest_worker_job_done",
                job_id=str(result.job_id),
                status=result.status.value,
                asset_class=result.asset_class,
                chunk_count=result.chunk_count,
            )
            if once:
                return
        except DocumentIngestError as exc:
            logger.warning("rag_ingest_worker_job_error", error=str(exc))
            if once:
                raise
            await asyncio.sleep(poll_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.exception("rag_ingest_worker_unexpected", error=str(exc))
            if once:
                raise
            await asyncio.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG document ingest worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued job then exit",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="Idle poll interval when looping",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(_loop(once=args.once, poll_seconds=args.poll_seconds))
    except KeyboardInterrupt:
        logger.info("rag_ingest_worker_stopped")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
