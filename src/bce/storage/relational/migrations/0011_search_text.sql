-- Search text, hybrid-v4: the whole symbol is searchable, not just its first 1200 characters.
--
-- Measured on a React/TypeScript front end: 27 of 31 expected files that retrieval missed at K=50
-- never entered the candidate pool at all. Their evidence lived in the parts of a symbol nothing
-- indexed - a toast string 60 lines into a page component, a `localStorage.getItem('token')` in
-- the middle of a 10 KB panel, the literal members of a `type` union or a constant array. Both
-- search indexes read only the head of a symbol:
--
--   * the FTS document carried name / signature / docstring, never the body;
--   * the embedding content carried a 1200-character body snippet, one vector per symbol.
--
-- This migration gives both indexes the symbol's *full* text:
--
--   1. ``symbol_fts.body`` - the extractor's search text (declaration + body, capped upstream),
--      folded into the FTS document at weight D next to signature + docstring. The document's
--      C part now carries every word of the file *path* (``settings sections Repos Section``),
--      not just the file stem, so a task that says "the settings sections folder" has something
--      to match.
--   2. ``embeddings.chunk`` - a symbol longer than one embedding window is stored as several
--      rows (chunk 0, 1, 2 ...), each with the same header (name, kind, path, signature,
--      docstring) and a window of the body. Nearest-neighbour search collapses the rows back to
--      one symbol at the best chunk's rank. The old ``UNIQUE (kind, ref_id)`` becomes
--      ``UNIQUE (kind, ref_id, chunk)``; existing rows keep ``chunk = 0``.
--
-- Rows already stored keep working (body NULL, chunk 0); a reindex fills them in.

-- "repo:src/components/settings/sections/ReposSection.tsx"
--   -> "src components settings sections ReposSection"
CREATE OR REPLACE FUNCTION bce_file_path_words(file_id TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT regexp_replace(
        regexp_replace(regexp_replace(file_id, '^[^:/\\]*:', ''), '\.[^./\\]*$', ''),
        '[/\\._-]+', ' ', 'g'
    )
$$;

ALTER TABLE symbol_fts ADD COLUMN IF NOT EXISTS body TEXT;

ALTER TABLE symbol_fts DROP COLUMN IF EXISTS document;

ALTER TABLE symbol_fts ADD COLUMN document tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple', coalesce(name, '')), 'A')
    || setweight(to_tsvector('simple', bce_identifier_words(coalesce(name, ''))), 'B')
    || setweight(
        to_tsvector(
            'simple',
            bce_identifier_words(bce_container_of(symbol_id)) || ' '
                || bce_identifier_words(bce_file_path_words(coalesce(file_id, '')))
        ),
        'C'
    )
    || setweight(
        to_tsvector(
            'simple',
            coalesce(signature, '') || ' ' || coalesce(docstring, '') || ' '
                || bce_identifier_words(coalesce(body, ''))
        ),
        'D'
    )
) STORED;

CREATE INDEX IF NOT EXISTS idx_symbol_fts_document ON symbol_fts USING gin (document);

ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS chunk INTEGER NOT NULL DEFAULT 0;

DO $$
DECLARE
    con TEXT;
BEGIN
    FOR con IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'embeddings'::regclass AND contype = 'u'
    LOOP
        EXECUTE format('ALTER TABLE embeddings DROP CONSTRAINT %I', con);
    END LOOP;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_kind_ref_chunk
    ON embeddings (kind, ref_id, chunk);
