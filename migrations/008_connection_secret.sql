-- OAuth credentials for the hosted connect page. The ciphertext is the secret.
-- weave_app is not granted this table. The webhook uses the owner connection.
-- Safe to run twice.

CREATE TABLE IF NOT EXISTS connection_secret (
    tenant_id         text NOT NULL,
    provider          text NOT NULL,
    external_id       text NOT NULL DEFAULT '',
    secret_encrypted  text NOT NULL,
    scopes            text NOT NULL DEFAULT '',
    status            text NOT NULL DEFAULT 'connected',
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, provider)
);
