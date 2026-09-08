-- Extensions and the AGE code graph.
-- Single DB (P5): graph + vectors + relational + audit all live here.

CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;

LOAD 'age';
-- SET LOCAL so the ag_catalog search_path stays scoped to this migration's transaction and does
-- not leak into later migrations (otherwise relational tables get created in ag_catalog, not public).
SET LOCAL search_path = ag_catalog, "$user", public;

-- Create the code graph once. create_graph raises if it already exists, so guard it.
DO $$
BEGIN
    PERFORM create_graph('code_graph');
EXCEPTION
    WHEN others THEN
        -- Graph already exists; nothing to do.
        NULL;
END
$$;
