"""Deterministic PDF text extraction (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int  # 1-based
    text: str


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    pages: list[ExtractedPage]
    page_count: int

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text.strip())


def _pymupdf() -> Any:
    try:
        import pymupdf
    except ImportError as exc:
        raise ImportError(
            "pymupdf is not installed. Run: uv sync --extra rag"
        ) from exc
    return pymupdf


def extract_pdf_bytes(data: bytes) -> ExtractedDocument:
    """Extract plain text from a PDF byte payload."""
    pymupdf = _pymupdf()
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        pages: list[ExtractedPage] = []
        for index in range(doc.page_count):
            page = doc.load_page(index)
            text = page.get_text("text") or ""
            pages.append(ExtractedPage(page_number=index + 1, text=text.strip()))
        return ExtractedDocument(pages=pages, page_count=doc.page_count)
    finally:
        doc.close()


@dataclass(frozen=True, slots=True)
class TextChunk:
    chunk_index: int
    page_number: int | None
    content: str


def chunk_pages(
    pages: list[ExtractedPage],
    *,
    chunk_size: int,
    chunk_overlap: int,
    min_chars: int = 40,
) -> list[TextChunk]:
    """
    Fixed-size character chunking with overlap.

    Deterministic — never use an LLM for splitting.
    """
    if chunk_size < 100:
        raise ValueError("chunk_size must be >= 100")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be in [0, chunk_size)")

    chunks: list[TextChunk] = []
    for page in pages:
        text = " ".join(page.text.split())
        if len(text) < min_chars:
            continue
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            piece = text[start:end].strip()
            if len(piece) >= min_chars:
                chunks.append(
                    TextChunk(
                        chunk_index=len(chunks),
                        page_number=page.page_number,
                        content=piece,
                    )
                )
            if end >= len(text):
                break
            start = end - chunk_overlap
    return chunks
