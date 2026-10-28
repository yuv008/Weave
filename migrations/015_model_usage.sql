-- Requests per company per day on the server's model key. The agent counts one
-- /answer or /wisp/mail per row update; a company with its own key is not counted.
-- Only the owner connection reads it. Safe to run twice.

CREATE TABLE IF NOT EXISTS model_usage (
    tenant_id text NOT NULL,
    day       date NOT NULL DEFAULT current_date,
    calls     integer NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);
