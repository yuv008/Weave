-- Email verification, password reset, and company invites.
-- The owner connection applies this. weave_app is not granted these tables.
-- Safe to run twice: the backfill only touches accounts made before the cutoff.

ALTER TABLE app_user ADD COLUMN IF NOT EXISTS email_verified_at timestamptz;

-- Accounts that existed before verification shipped count as verified.
UPDATE app_user SET email_verified_at = created_at
WHERE email_verified_at IS NULL AND created_at < '2026-10-29 00:00:00+00';

-- One-time links mailed to an account: verify the address, or reset the password.
CREATE TABLE IF NOT EXISTS email_token (
    token_sha256 text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES app_user (id),
    purpose      text NOT NULL CHECK (purpose IN ('verify', 'reset')),
    expires_at   timestamptz NOT NULL,
    used_at      timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS email_token_user ON email_token (user_id);

-- An owner's invite to join their company as a member.
CREATE TABLE IF NOT EXISTS invite (
    token_sha256 text PRIMARY KEY,
    tenant_id    text NOT NULL,
    email        text NOT NULL,
    role         text NOT NULL DEFAULT 'member' CHECK (role = 'member'),
    invited_by   text NOT NULL REFERENCES app_user (id),
    expires_at   timestamptz NOT NULL,
    accepted_at  timestamptz,
    revoked_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS invite_tenant ON invite (tenant_id);
