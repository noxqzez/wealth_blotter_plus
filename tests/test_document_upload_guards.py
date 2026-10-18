"""Unit tests for RAG upload kill switch / rate / page quota."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.document_upload_guards import (
    DocumentUploadDisabledError,
    DocumentUploadQuotaError,
    DocumentUploadRateLimitError,
    _rate_windows,
    enforce_rag_upload_guards,
)


def _settings(**overrides: object) -> MagicMock:
    cfg = MagicMock()
    cfg.rag_upload_enabled = True
    cfg.rag_upload_rate_limit_per_minute = 5
    cfg.rag_upload_max_pages_per_day = 50
    cfg.rag_upload_max_pages_per_file = 50
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture(autouse=True)
def _clear_rate_windows() -> None:
    _rate_windows.clear()


def test_kill_switch_blocks_upload() -> None:
    cfg = _settings(rag_upload_enabled=False)
    with pytest.raises(DocumentUploadDisabledError):
        enforce_rag_upload_guards(
            advisor_id=uuid4(),
            content=b"%PDF-1.4",
            settings=cfg,
        )


def test_rejects_file_over_page_cap() -> None:
    cfg = _settings(rag_upload_max_pages_per_file=10)
    with (
        patch(
            "backend.app.services.document_upload_guards.count_pdf_pages",
            return_value=11,
        ),
        patch(
            "backend.app.services.document_upload_guards._pages_used_today",
            return_value=0,
        ),
        pytest.raises(DocumentUploadQuotaError, match="max per file"),
    ):
        enforce_rag_upload_guards(
            advisor_id=uuid4(),
            content=b"%PDF",
            settings=cfg,
        )


def test_rejects_when_daily_quota_exceeded() -> None:
    cfg = _settings(rag_upload_max_pages_per_day=50)
    with (
        patch(
            "backend.app.services.document_upload_guards.count_pdf_pages",
            return_value=20,
        ),
        patch(
            "backend.app.services.document_upload_guards._pages_used_today",
            return_value=40,
        ),
        pytest.raises(DocumentUploadQuotaError, match="Daily page quota"),
    ):
        enforce_rag_upload_guards(
            advisor_id=uuid4(),
            content=b"%PDF",
            settings=cfg,
        )


def test_allows_within_quota() -> None:
    cfg = _settings()
    with (
        patch(
            "backend.app.services.document_upload_guards.count_pdf_pages",
            return_value=10,
        ),
        patch(
            "backend.app.services.document_upload_guards._pages_used_today",
            return_value=5,
        ),
    ):
        pages = enforce_rag_upload_guards(
            advisor_id=uuid4(),
            content=b"%PDF",
            settings=cfg,
        )
    assert pages == 10


def test_rate_limit_per_minute() -> None:
    cfg = _settings(rag_upload_rate_limit_per_minute=2)
    advisor = uuid4()
    with (
        patch(
            "backend.app.services.document_upload_guards.count_pdf_pages",
            return_value=1,
        ),
        patch(
            "backend.app.services.document_upload_guards._pages_used_today",
            return_value=0,
        ),
    ):
        enforce_rag_upload_guards(advisor_id=advisor, content=b"%PDF", settings=cfg)
        enforce_rag_upload_guards(advisor_id=advisor, content=b"%PDF", settings=cfg)
        with pytest.raises(DocumentUploadRateLimitError):
            enforce_rag_upload_guards(
                advisor_id=advisor, content=b"%PDF", settings=cfg
            )


def test_pages_used_today_sums_rows() -> None:
    from backend.app.services.document_upload_guards import _pages_used_today

    mock_table = MagicMock()
    mock_table.select.return_value.eq.return_value.gte.return_value.execute.return_value = (
        MagicMock(data=[{"page_count": 10}, {"page_count": 5}, {"page_count": None}])
    )
    with patch(
        "backend.app.services.document_upload_guards.service_table",
        return_value=mock_table,
    ):
        total = _pages_used_today(uuid4(), day_start=datetime.now(UTC))
    assert total == 15
