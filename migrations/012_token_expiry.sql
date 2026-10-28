-- Viewer tokens expire. New tokens get 30 days from app.connect.routes; tokens
-- minted before this get 30 days from when they were made.
-- The owner connection applies this. Safe to run twice.

ALTER TABLE api_token ADD COLUMN IF NOT EXISTS expires_at timestamptz;

UPDATE api_token SET expires_at = created_at + interval '30 days' WHERE expires_at IS NULL;

-- Same body as 007 plus the expiry check. CREATE OR REPLACE keeps the grant to weave_app.
CREATE OR REPLACE FUNCTION weave_token_owner(token_hash text)
RETURNS TABLE (tenant_id text, person_id text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT t.tenant_id, t.person_id
    FROM api_token AS t
    WHERE t.token_sha256 = weave_token_owner.token_hash
      AND t.revoked_at IS NULL
      AND (t.expires_at IS NULL OR t.expires_at > now())
    LIMIT 1;
END;
$$;
