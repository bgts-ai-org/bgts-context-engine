-- Pre-create the vertex labels and index their properties BEFORE the first index run.
--
-- AGE creates label tables lazily on the first MERGE and gives them only a primary key on id.
-- Every edge upsert does `MATCH (a {gid: $src}), (b {gid: $dst})`, which the planner rewrites to
-- `properties @> '{"gid": ...}'`; without a GIN index that is a sequential scan of every vertex
-- table, so indexing degrades quadratically with graph size (measured: ~24 ms per edge on a
-- 15k-symbol graph versus ~2 ms with the index). Creating the labels and indexes here makes the
-- lookups indexed from the very first row. fastupdate=off keeps lookups from having to scan a
-- GIN pending list during bulk inserts, which is exactly when we rely on them.
--
-- Idempotent: labels already present (databases indexed before this migration) are skipped and
-- CREATE INDEX IF NOT EXISTS is a no-op when the index was added by hand earlier.

LOAD 'age';
SET LOCAL search_path = ag_catalog, "$user", public;

DO $$
DECLARE
    label TEXT;
BEGIN
    FOREACH label IN ARRAY ARRAY['Repo', 'File', 'Symbol', 'Module', 'Route', 'DesignNote'] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM ag_catalog.ag_label l
            JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
            WHERE g.name = 'code_graph' AND l.name = label
        ) THEN
            -- create_vlabel takes cstring arguments; a text variable needs the explicit casts.
            PERFORM ag_catalog.create_vlabel('code_graph'::cstring, label::cstring);
        END IF;
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON code_graph.%I USING gin (properties) WITH (fastupdate = off)',
            lower(label) || '_properties_gin',
            label
        );
    END LOOP;
END
$$;
