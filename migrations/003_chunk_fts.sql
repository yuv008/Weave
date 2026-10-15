-- Keyword half of search_context. Safe to run twice.

ALTER TABLE chunk
    ADD COLUMN IF NOT EXISTS tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;

CREATE INDEX IF NOT EXISTS chunk_tsv ON chunk USING gin (tsv);
