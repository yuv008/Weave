-- Sources a tenant connected that are not listed in config/weave.yaml.
-- The owner connection applies this. Safe to run twice.

CREATE TABLE IF NOT EXISTS connected_source (
    tenant_id       text NOT NULL,
    acl_group       text NOT NULL,
    name            text NOT NULL,
    min_level       integer NOT NULL,
    cascade_policy  text NOT NULL,
    authority       integer NOT NULL,
    org_wide        boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, acl_group)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON connected_source TO weave_app;
