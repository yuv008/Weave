-- Shared Postgres contract. One owner applies migrations; both builders review.
-- Safe to run twice.

CREATE EXTENSION IF NOT EXISTS vector;

-- Caller groups. Strings use shared/acl.py, the same format as Item.acl_groups.
CREATE TABLE IF NOT EXISTS group_membership (
    tenant_id     text NOT NULL,
    person_id     text NOT NULL,
    acl_group     text NOT NULL,
    source_system text NOT NULL,
    PRIMARY KEY (tenant_id, person_id, acl_group)
);

-- Last good ACL sync per source. The gate excludes sources older than acl_stale_minutes.
CREATE TABLE IF NOT EXISTS source_sync (
    tenant_id         text NOT NULL,
    source_id         text NOT NULL,
    acl_group         text NOT NULL,
    last_good_sync_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, source_id)
);

-- Rises whenever any ACL snapshot changes; part of every cache key.
CREATE TABLE IF NOT EXISTS tenant_acl (
    tenant_id  text PRIMARY KEY,
    acl_epoch  bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Outbox written by the supersede step right after the graph write succeeds.
-- kind: superseded | retracted | contradicts.
CREATE TABLE IF NOT EXISTS change_event (
    id          text PRIMARY KEY,
    tenant_id   text NOT NULL,
    fact_id     text NOT NULL,
    kind        text NOT NULL,
    new_fact_id text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    processed   boolean NOT NULL DEFAULT false,
    UNIQUE (tenant_id, fact_id, kind)
);

CREATE INDEX IF NOT EXISTS change_event_pending
    ON change_event (tenant_id, created_at) WHERE NOT processed;

-- Chunks for search_context. Dimension matches config/weave.yaml embedding.dim.
CREATE TABLE IF NOT EXISTS chunk (
    id         text PRIMARY KEY,
    tenant_id  text NOT NULL,
    item_id    text NOT NULL,
    ordinal    integer NOT NULL DEFAULT 0,
    text       text NOT NULL,
    embedding  vector(384),
    acl_groups text[] NOT NULL,
    min_level  integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, item_id, ordinal)
);

CREATE INDEX IF NOT EXISTS chunk_item ON chunk (tenant_id, item_id);
CREATE INDEX IF NOT EXISTS chunk_acl ON chunk USING gin (acl_groups);
