-- Usage anchors: substring / word search over symbol bodies at index speed.
--
-- The usage anchor source (``anchors._usage_anchors``) looks for the code fragments a task wrote
-- out - ``localStorage``, ``'token'``, ``error?.response?.data?.message`` - *inside*
-- ``symbol_fts.body``. Names find the thing; usage finds the places that use the thing, which is
-- what "every place that reads the raw key" asks for. Without an index each fragment is a
-- sequential scan over every body (fine at 4k symbols, seconds at 100k).
--
-- ``pg_trgm``'s GIN operator class accelerates both operators the lookup uses: ``LIKE`` for
-- substrings and ``~`` for the word-bounded identifier regex. It is a contrib module that
-- standard PostgreSQL builds ship; where it is missing (a stripped image) the index is skipped
-- and the lookup still works, unaccelerated.
--
-- The same index on ``file_id`` serves the path anchor source (``files_by_suffix``): a task that
-- names ``src/utils/auth.ts`` or pastes a traceback frame is matched against file ids by suffix
-- (``ILIKE '%/utils/auth.ts'``), which is a trigram lookup too.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS idx_symbol_fts_body_trgm
            ON symbol_fts USING gin (body gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_symbol_fts_file_id_trgm
            ON symbol_fts USING gin (file_id gin_trgm_ops);
    ELSE
        RAISE NOTICE 'pg_trgm unavailable: usage anchors run without a body index';
    END IF;
END $$;
