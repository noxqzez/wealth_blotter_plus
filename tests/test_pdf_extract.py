"""Unit tests for deterministic PDF chunking (no Hugging Face / DB)."""

from __future__ import annotations

from backend.app.services.pdf_extract import ExtractedPage, chunk_pages


def test_chunk_pages_produces_overlapping_chunks() -> None:
    text = ("alpha beta gamma delta " * 40).strip()
    pages = [ExtractedPage(page_number=1, text=text)]
    chunks = chunk_pages(pages, chunk_size=120, chunk_overlap=20, min_chars=20)
    assert len(chunks) >= 2
    assert chunks[0].chunk_index == 0
    assert chunks[0].page_number == 1
    assert all(len(c.content) >= 20 for c in chunks)


def test_chunk_pages_skips_tiny_pages() -> None:
    pages = [ExtractedPage(page_number=1, text="too short")]
    chunks = chunk_pages(pages, chunk_size=200, chunk_overlap=40, min_chars=40)
    assert chunks == []
