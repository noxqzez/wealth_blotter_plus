-- Additive: page_count on document_ingest_jobs for daily RAG upload quotas.
-- Safe to run repeatedly (IF NOT EXISTS pattern via DO block).

ALTER TABLE document_ingest_jobs
    ADD COLUMN IF NOT EXISTS page_count INTEGER
    CHECK (page_count IS NULL OR page_count >= 0);

CREATE INDEX IF NOT EXISTS idx_document_ingest_jobs_advisor_created
    ON document_ingest_jobs (advisor_id, created_at);

COMMENT ON COLUMN document_ingest_jobs.page_count IS
    'PDF page count at upload time; used for RAG_UPLOAD_MAX_PAGES_PER_DAY quota.';
