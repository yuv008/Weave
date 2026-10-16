-- Ingest checkpoints. One owner applies migrations; both builders review.
-- Safe to run twice. SyncState, SyncFailure, and SlackEvent leave FalkorDB.

CREATE TABLE IF NOT EXISTS ingest_record (
    id         text PRIMARY KEY,
    tenant_id  text NOT NULL,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ingest_record_kind ON ingest_record (tenant_id, kind);
