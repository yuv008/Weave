-- Agent memory: conversations, episodes, playbooks, tasks. Owned by the agents side. Safe to run twice.
-- Memory holds node ids and short notes, never company facts. Every recalled
-- id is re-gated on read, so a row here never grants access by itself.

CREATE TABLE IF NOT EXISTS conversations (
    id          text PRIMARY KEY,
    tenant_id   text NOT NULL,
    person_id   text NOT NULL,
    surface     text NOT NULL,
    title       text NOT NULL DEFAULT '',
    summary     text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations (tenant_id, person_id, created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id               bigserial PRIMARY KEY,
    conversation_id  text NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role             text NOT NULL CHECK (role IN ('user', 'agent')),
    text             text NOT NULL,
    node_ids         text[] NOT NULL DEFAULT '{}',
    at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversation_turns_conv ON conversation_turns (conversation_id, id);

CREATE TABLE IF NOT EXISTS episodes (
    id               text PRIMARY KEY,
    tenant_id        text NOT NULL,
    person_id        text NOT NULL,
    agent            text NOT NULL,
    conversation_id  text,
    kind             text NOT NULL DEFAULT 'answer' CHECK (kind IN ('answer', 'note')),
    question         text NOT NULL DEFAULT '',
    intent           text,
    tools            jsonb NOT NULL DEFAULT '[]',
    node_ids         text[] NOT NULL DEFAULT '{}',
    note             text NOT NULL DEFAULT '',
    outcome          text NOT NULL DEFAULT 'ok',
    feedback         jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', question || ' ' || note)) STORED,
    embedding        vector(384)
);
CREATE INDEX IF NOT EXISTS episodes_namespace ON episodes (tenant_id, person_id, agent, created_at DESC);
CREATE INDEX IF NOT EXISTS episodes_tsv ON episodes USING gin (tsv);

CREATE TABLE IF NOT EXISTS playbooks (
    id           bigserial PRIMARY KEY,
    tenant_id    text,
    role         text NOT NULL,
    intent       text NOT NULL,
    version      int NOT NULL DEFAULT 1,
    steps        jsonb NOT NULL,
    approved_by  text,
    active       boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS playbooks_version
    ON playbooks (coalesce(tenant_id, ''), role, intent, version);

CREATE TABLE IF NOT EXISTS tasks (
    id            text PRIMARY KEY,
    tenant_id     text NOT NULL,
    kind          text NOT NULL,
    target_agent  text,
    node_ids      text[] NOT NULL DEFAULT '{}',
    payload       jsonb NOT NULL DEFAULT '{}',
    state         text NOT NULL,
    created_by    text,
    episode_id    text,
    parent_id     text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    claimed_at    timestamptz,
    claimed_by    text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks (tenant_id, kind, target_agent, state, created_at);

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'tool';
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS tools text[] NOT NULL DEFAULT '{}';
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS conversation_id text;
