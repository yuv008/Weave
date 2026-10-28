-- One owner per company. Signup checks first; this index decides when two
-- signups for the same company run at once.
-- The owner connection applies this. Safe to run twice.

CREATE UNIQUE INDEX IF NOT EXISTS tenant_membership_one_owner
    ON tenant_membership (tenant_id)
    WHERE role = 'owner';
