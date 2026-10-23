-- Pre-create the edge labels BEFORE the first index run.
--
-- AGE creates an edge label's table lazily, when the first edge carrying that label is written.
-- Until then the label does not exist, which bites in two places:
--
--   * Raw SQL that names an edge table fails with UndefinedTable. symbol_features reads
--     "code_graph"."CALLS" directly, so context retrieval over an index without a single call
--     edge (a small repository, or one that only declares types and constants) raised
--     `relation "code_graph.CALLS" does not exist` and the API answered 500. Cypher is not
--     affected: a MATCH on a missing label simply returns no rows.
--   * The first edge MERGE with a given label creates its table and indexes inside the long
--     indexing transaction. Two indexers started together on a fresh database both try to create
--     the same label: one waits for the other to commit and then fails with DuplicateTable, or
--     the two deadlock, and the loser's whole run is lost.
--
-- Creating the labels here removes both. create_elabel raises when the label already exists, so
-- labels that are present (databases indexed before this migration) are skipped, exactly as in
-- 0008. The tables come out identical to lazily created ones (btree indexes on start_id and
-- end_id). An edge label added to EdgeLabel later needs a migration of its own that creates it
-- the same way: databases that have applied this file never run it again.

LOAD 'age';
SET LOCAL search_path = ag_catalog, "$user", public;

DO $$
DECLARE
    label TEXT;
BEGIN
    FOREACH label IN ARRAY ARRAY[
        'DEFINED_IN', 'BELONGS_TO', 'IMPORTS', 'CALLS', 'INHERITS', 'IMPLEMENTS', 'REFERENCES',
        'ROUTES_TO', 'EXPLAINS'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM ag_catalog.ag_label l
            JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
            WHERE g.name = 'code_graph' AND l.name = label
        ) THEN
            -- create_elabel takes cstring arguments; a text variable needs the explicit casts.
            PERFORM ag_catalog.create_elabel('code_graph'::cstring, label::cstring);
        END IF;
    END LOOP;
END
$$;
