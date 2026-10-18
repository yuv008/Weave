-- Login accounts, company membership, and browser sessions.
-- The owner connection applies this. weave_app is not granted these tables.
-- Safe to run twice.

CREATE TABLE IF NOT EXISTS app_user (
    id            text PRIMARY KEY,
    email         text NOT NULL UNIQUE,
    name          text NOT NULL,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_membership (
    user_id   text NOT NULL REFERENCES app_user (id),
    tenant_id text NOT NULL,
    role      text NOT NULL,
    PRIMARY KEY (user_id, tenant_id)
);

CREATE TABLE IF NOT EXISTS app_session (
    token_sha256 text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES app_user (id),
    tenant_id    text NOT NULL,
    expires_at   timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS app_session_user ON app_session (user_id);

CREATE TABLE IF NOT EXISTS login_attempt (
    email          text PRIMARY KEY,
    failures       integer NOT NULL DEFAULT 0,
    window_started timestamptz NOT NULL DEFAULT now()
);
