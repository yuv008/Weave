-- One row per agent tool call. Owned by the agents side. Safe to run twice.
-- hidden counts what the gate filtered out; it is never shown to the caller
-- unless hidden_sources_notice is on.

CREATE TABLE IF NOT EXISTS audit_log (
    id               bigserial PRIMARY KEY,
    tenant_id        text NOT NULL,
    request_id       text NOT NULL,
    at               timestamptz NOT NULL DEFAULT now(),
    surface          text NOT NULL,
    principal_kind   text NOT NULL,
    person_id        text,
    channel_group    text,
    level            int NOT NULL,
    visibility_hash  text NOT NULL,
    tool             text NOT NULL,
    args             jsonb NOT NULL,
    node_ids         text[] NOT NULL DEFAULT '{}',
    hidden           int NOT NULL DEFAULT 0,
    outcome          text NOT NULL,
    ms               int NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_log_tenant_at ON audit_log (tenant_id, at DESC);
CREATE INDEX IF NOT EXISTS audit_log_request ON audit_log (request_id);
