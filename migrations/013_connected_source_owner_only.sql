-- connected_source has no tenant policy. Only the owner connection reads it
-- (app.graph.policy uses connect_pg), so weave_app loses the grant from 010.
-- The owner connection applies this. Safe to run twice.

REVOKE ALL ON connected_source FROM weave_app;
