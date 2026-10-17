-- Row level security for tenant tables. Safe to run twice.
-- weave_app is NOLOGIN and does not bypass row level security (the default).
-- Policies are not forced on the table owner, so migrations, seeds, and
-- owner connections used by tests still see every row.
-- App code switches with SET LOCAL ROLE weave_app and
-- set_config('weave.tenant', <tenant>, true) inside the same transaction.
-- That setting is local, so a transaction-mode pooler cannot leak it.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'weave_app') THEN
        CREATE ROLE weave_app NOLOGIN;
    END IF;
END
$$;

ALTER ROLE weave_app NOLOGIN;
GRANT weave_app TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO weave_app;

ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS tenant_id text;

UPDATE conversation_turns AS turn
SET tenant_id = conv.tenant_id
FROM conversations AS conv
WHERE turn.conversation_id = conv.id
  AND turn.tenant_id IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM conversation_turns WHERE tenant_id IS NULL) THEN
        ALTER TABLE conversation_turns ALTER COLUMN tenant_id SET NOT NULL;
    END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    group_membership,
    source_sync,
    tenant_acl,
    change_event,
    chunk,
    api_token,
    ingest_record,
    audit_log,
    conversations,
    conversation_turns,
    episodes,
    tasks,
    slack_reply,
    playbooks
TO weave_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO weave_app;

-- Token lookup happens before the tenant is known, so it cannot use the
-- tenant policy. The owner runs this and bypasses row level security.
-- plpgsql is not inlined, so SECURITY DEFINER holds for weave_app too.
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
    LIMIT 1;
END;
$$;

REVOKE ALL ON FUNCTION weave_token_owner(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION weave_token_owner(text) TO weave_app;

DO $$
DECLARE
    tbl text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'group_membership',
        'source_sync',
        'tenant_acl',
        'change_event',
        'chunk',
        'api_token',
        'ingest_record',
        'audit_log',
        'conversations',
        'conversation_turns',
        'episodes',
        'tasks',
        'slack_reply'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS weave_tenant ON %I', tbl);
        EXECUTE format(
            $policy$CREATE POLICY weave_tenant ON %I FOR ALL TO weave_app USING (tenant_id = current_setting('weave.tenant', true)) WITH CHECK (tenant_id = current_setting('weave.tenant', true))$policy$,
            tbl
        );
    END LOOP;
END
$$;

ALTER TABLE playbooks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS weave_tenant ON playbooks;
CREATE POLICY weave_tenant ON playbooks
    FOR ALL
    TO weave_app
    USING (tenant_id = current_setting('weave.tenant', true) OR tenant_id IS NULL)
    WITH CHECK (tenant_id = current_setting('weave.tenant', true) OR tenant_id IS NULL);
