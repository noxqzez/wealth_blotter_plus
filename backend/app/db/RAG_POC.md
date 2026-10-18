# RAG research documents — POC

Upload in-house research PDFs → Supabase Storage → CrewAI classification →
Hugging Face embeddings → `pgvector` retrieval for investment agents.

## What ships

| Piece | Path |
|-------|------|
| Additive SQL | `migration_rag_poc.sql` (+ included in `schema.sql`) |
| Upload / status / retrieve API | `backend/app/api/v1/documents.py` |
| Ingest pipeline | `backend/app/services/document_ingest.py` |
| PDF extract + chunk | `backend/app/services/pdf_extract.py` |
| HF embeddings | `backend/app/services/embeddings.py` |
| Similarity search | `backend/app/services/rag_retrieve.py` |
| Classification crew | `backend/app/agents/doc_classify.py` + YAML |
| Agent retrieval helper | `backend/app/agents/tools/rag_retrieval_tool.py` |
| Optional poll worker | `backend/app/agents/rag_ingest_worker.py` |

## Setup

1. **Enable `vector`** in Supabase (Database → Extensions).
2. **Create private Storage bucket** named `research-documents` (or match `RAG_STORAGE_BUCKET`).
3. **Apply migrations:**

```bash
# Supabase SQL editor, or:
psql "$DATABASE_URL" -f backend/app/db/migration_rag_poc.sql
psql "$DATABASE_URL" -f backend/app/db/migration_rag_upload_guards.sql
```

### Pilot upload guards (option B)

| Env | Default | Purpose |
|-----|---------|---------|
| `RAG_UPLOAD_ENABLED` | `true` | Kill switch — set `false` to block all uploads |
| `RAG_UPLOAD_RATE_LIMIT_PER_MINUTE` | `5` | Per-advisor rolling 1-minute cap |
| `RAG_UPLOAD_MAX_PAGES_PER_DAY` | `50` | Per-advisor UTC daily page budget |
| `RAG_UPLOAD_MAX_PAGES_PER_FILE` | `50` | Max pages in a single PDF |

Page counts are read from the PDF at upload and stored on `document_ingest_jobs.page_count`.

4. **Install RAG deps** (pulls `sentence-transformers` / PyMuPDF / multipart):

```bash
uv sync --extra rag
```

5. Copy RAG keys from `.env.example` into `.env`. Keep `DEV_AUTH_BYPASS=true` for local API tests.

Default embedding model: `sentence-transformers/all-MiniLM-L6-v2` (**384 dims**).
Schema `vector(384)` must match `RAG_EMBEDDING_DIMS`. To upgrade to
`BAAI/bge-base-en-v1.5` (768), alter the column + RPC and re-embed everything.

## Flow

```text
POST /api/v1/documents/upload
  → Storage object {advisor_id}/{job_id}/{file}.pdf
  → document_ingest_jobs (queued)
  → (optional) background process

process:
  download → PyMuPDF text → CrewAI classify
    → Neither  → document_registry quarantined (no chunks)
    → Stock/Crypto → chunk → HF embed → document_chunks
  → job status ready | quarantined | failed

POST /api/v1/documents/retrieve
  → embed query → match_document_chunks (advisor + optional ticker/asset_class)
```

Classification labels: `US_Stock` | `HK_Stock` | `Crypto` | `Neither`.

## API quick test

```bash
uv run uvicorn backend.app.main:app --reload --port 8000

curl -s -X POST "http://localhost:8000/api/v1/documents/upload" \
  -F "file=@./samples/some_research.pdf"

# If auto-process disabled:
curl -s -X POST "http://localhost:8000/api/v1/documents/jobs/{JOB_ID}/process"

curl -s "http://localhost:8000/api/v1/documents/jobs/{JOB_ID}"

curl -s -X POST "http://localhost:8000/api/v1/documents/retrieve" \
  -H "Content-Type: application/json" \
  -d '{"query":"thesis on AI semiconductors","ticker":"NVDA","top_k":5}'
```

Optional worker (when `RAG_AUTO_PROCESS_UPLOAD=false`):

```bash
uv run python -m backend.app.agents.rag_ingest_worker --once
# or loop:
uv run python -m backend.app.agents.rag_ingest_worker --poll-seconds 5
```

## Agent usage

```python
from uuid import UUID
from backend.app.agents.tools.rag_retrieval_tool import lookup_research_context

ctx = await lookup_research_context(
    advisor_id=UUID("..."),
    query="key risks in the latest note",
    ticker="0700.HK",
    asset_class="HK_Stock",
    top_k=5,
)
```

Wire this tool into future analysis crews; advisory crew is unchanged in this POC.

## POC limits

- Text PDFs only (no OCR).
- Single embedding model/dim pair; change requires migration + re-embed.
- Live market numbers should stay in APIs/tools — not in static vectors.
- Rules-as-code stay in `configs` jsonb — never in Storage.
- Classification uses the same Ollama/cloud LLM flags as advisory.
