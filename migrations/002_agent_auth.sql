-- Per-user API tokens for MCP and the web UI. Only SHA-256 hashes are stored.
-- Safe to run twice.

CREATE TABLE IF NOT EXISTS api_token (
    token_sha256 text PRIMARY KEY,
    tenant_id    text NOT NULL,
    person_id    text NOT NULL,
    label        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz
);

CREATE INDEX IF NOT EXISTS api_token_person ON api_token (tenant_id, person_id) WHERE revoked_at IS NULL;
