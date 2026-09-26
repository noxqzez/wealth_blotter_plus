-- Additive RAG POC migration: research document ingest + pgvector chunks.
-- Safe to run on an existing MVP DB (IF NOT EXISTS / DROP POLICY IF EXISTS).
-- Does not alter portfolio MVP tables.
--
-- Prerequisites:
--   1. Enable pgvector (Supabase: Database → Extensions → vector)
--   2. Create Storage bucket `research-documents` (private) in Supabase Dashboard
--
-- Embedding default: sentence-transformers/all-MiniLM-L6-v2 → vector(384).
-- Changing the model requires a matching dimension + full re-embed.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ---------------------------------------------------------------------------
-- Ingest job queue (upload API enqueues; worker / process endpoint drains)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_ingest_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id          UUID NOT NULL,
    storage_bucket      TEXT NOT NULL,
    storage_path        TEXT NOT NULL,
    original_filename   TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN (
                            'queued',
                            'processing',
                            'ready',
                            'quarantined',
                            'failed'
                        )),
    error_message       TEXT,
    document_id         UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    UNIQUE (storage_bucket, storage_path)
);

CREATE INDEX IF NOT EXISTS idx_document_ingest_jobs_status
    ON document_ingest_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_document_ingest_jobs_advisor_id
    ON document_ingest_jobs (advisor_id);

-- ---------------------------------------------------------------------------
-- Document registry (classification metadata)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_registry (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    advisor_id                  UUID NOT NULL,
    ingest_job_id               UUID REFERENCES document_ingest_jobs (id) ON DELETE SET NULL,
    storage_bucket              TEXT NOT NULL,
    storage_path                TEXT NOT NULL,
    original_filename           TEXT NOT NULL,
    asset_class                 TEXT NOT NULL
                                CHECK (asset_class IN (
                                    'US_Stock',
                                    'HK_Stock',
                                    'Crypto',
                                    'Neither'
                                )),
    ticker                      TEXT,
    source_firm                 TEXT,
    overall_sentiment           TEXT
                                CHECK (overall_sentiment IS NULL OR overall_sentiment IN (
                                    'Bullish',
                                    'Bearish',
                                    'Neutral'
                                )),
    classification_confidence   DOUBLE PRECISION
                                CHECK (
                                    classification_confidence IS NULL
                                    OR (
                                        classification_confidence >= 0.0
                                        AND classification_confidence <= 1.0
                                    )
                                ),
    status                      TEXT NOT NULL DEFAULT 'indexed'
                                CHECK (status IN ('indexed', 'quarantined')),
    embedding_model             TEXT,
    processed_at                TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (storage_bucket, storage_path)
);

CREATE INDEX IF NOT EXISTS idx_document_registry_advisor_id
    ON document_registry (advisor_id);
CREATE INDEX IF NOT EXISTS idx_document_registry_ticker
    ON document_registry (ticker)
    WHERE ticker IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_document_registry_asset_class
    ON document_registry (asset_class);

ALTER TABLE document_ingest_jobs
    DROP CONSTRAINT IF EXISTS document_ingest_jobs_document_id_fkey;
ALTER TABLE document_ingest_jobs
    ADD CONSTRAINT document_ingest_jobs_document_id_fkey
    FOREIGN KEY (document_id) REFERENCES document_registry (id) ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- Chunks + embeddings (384 dims = all-MiniLM-L6-v2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES document_registry (id) ON DELETE CASCADE,
    advisor_id      UUID NOT NULL,
    chunk_index     INT NOT NULL,
    page_number     INT,
    content         TEXT NOT NULL,
    embedding       vector(384) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now()),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id
    ON document_chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_advisor_id
    ON document_chunks (advisor_id);

-- HNSW for cosine distance; create after first data load if needed on large corpora
CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw
    ON document_chunks
    USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Similarity search RPC (filtered by advisor / ticker / asset_class)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION match_document_chunks(
    query_embedding vector(384),
    match_count int DEFAULT 5,
    filter_advisor_id uuid DEFAULT NULL,
    filter_ticker text DEFAULT NULL,
    filter_asset_class text DEFAULT NULL
)
RETURNS TABLE (
    chunk_id uuid,
    document_id uuid,
    content text,
    chunk_index int,
    page_number int,
    ticker text,
    asset_class text,
    storage_path text,
    original_filename text,
    similarity float
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        c.id AS chunk_id,
        c.document_id,
        c.content,
        c.chunk_index,
        c.page_number,
        r.ticker,
        r.asset_class,
        r.storage_path,
        r.original_filename,
        (1.0 - (c.embedding <=> query_embedding))::float AS similarity
    FROM document_chunks c
    INNER JOIN document_registry r ON r.id = c.document_id
    WHERE r.status = 'indexed'
      AND r.asset_class <> 'Neither'
      AND (filter_advisor_id IS NULL OR c.advisor_id = filter_advisor_id)
      AND (filter_ticker IS NULL OR r.ticker = filter_ticker)
      AND (filter_asset_class IS NULL OR r.asset_class = filter_asset_class)
    ORDER BY c.embedding <=> query_embedding
    LIMIT GREATEST(match_count, 1);
$$;

-- ---------------------------------------------------------------------------
-- RLS (MVP writes use service-role; authenticated policies for future SSO)
-- ---------------------------------------------------------------------------
ALTER TABLE document_ingest_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS document_ingest_jobs_all_own ON document_ingest_jobs;
CREATE POLICY document_ingest_jobs_all_own ON document_ingest_jobs
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS document_registry_all_own ON document_registry;
CREATE POLICY document_registry_all_own ON document_registry
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

DROP POLICY IF EXISTS document_chunks_all_own ON document_chunks;
CREATE POLICY document_chunks_all_own ON document_chunks
    FOR ALL TO authenticated
    USING (advisor_id = auth.uid())
    WITH CHECK (advisor_id = auth.uid());

COMMENT ON TABLE document_ingest_jobs IS
    'RAG POC queue: PDF upload → classify → embed. Worker drains status=queued.';
COMMENT ON TABLE document_registry IS
    'RAG POC document metadata from classification agent (US/HK/Crypto/Neither).';
COMMENT ON TABLE document_chunks IS
    'RAG POC text chunks + pgvector embeddings (384d MiniLM by default).';
COMMENT ON FUNCTION match_document_chunks IS
    'Filtered cosine similarity over indexed research chunks for agent retrieval.';
