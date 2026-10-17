-- Slack answer replies. Owned by the agents side. Safe to run twice.
-- One row per Slack event_id, so a redelivery cannot start a second reply.
-- episodes.audience is "person" for a DM or web answer, or the channel
-- group (slack:C...) when the answer was built for a shared surface.

CREATE TABLE IF NOT EXISTS slack_reply (
    event_id    text PRIMARY KEY,
    tenant_id   text NOT NULL,
    channel_id  text NOT NULL,
    thread_ts   text NOT NULL DEFAULT '',
    state       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS audience text NOT NULL DEFAULT 'person';
