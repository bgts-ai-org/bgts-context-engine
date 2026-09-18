-- Lexical channel, hybrid-v3: identifier parts, container and file name in the FTS document.
--
-- The 'simple' parser splits snake_case ("calculate_cost" -> calculate, cost) but keeps a camelCase
-- or PascalCase identifier as one token, so on a C# / TypeScript codebase a task that says
-- "meeting" never matched ScheduledMeetingReconciler and the lexical anchor source was blind to
-- the very names it was meant to find. The document now carries four weighted parts:
--
--   A  the exact name                                  (ts_rank weight 1.0)
--   B  the name split at case changes                  (0.4)   ScheduledMeetingReconciler -> scheduled meeting reconciler
--   C  the container's and the file's name parts       (0.2)   a member of MeetingService in MeetingService.cs
--   D  signature + docstring                           (0.1)
--
-- so a symbol *named* after the term still ranks first inside a term's pool, a member of a class or
-- file named after it comes next, and a docstring mention last. Pure SQL, IMMUTABLE helpers, so the
-- generated column can be rebuilt on existing snapshots without re-indexing.

CREATE OR REPLACE FUNCTION bce_identifier_words(ident TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT regexp_replace(
        regexp_replace(ident, '([a-z0-9])([A-Z])', '\1 \2', 'g'),
        '([A-Z]+)([A-Z][a-z])', '\1 \2', 'g'
    )
$$;

-- "repo:path/to/MeetingService.cs" -> "MeetingService"
CREATE OR REPLACE FUNCTION bce_file_stem(file_id TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT regexp_replace(regexp_replace(file_id, '^.*[/\\:]', ''), '\.[^.]*$', '')
$$;

-- "csharp::Acme.Services::MongoDbService::GetAsync#abcd" -> "MongoDbService" ('' for top level)
CREATE OR REPLACE FUNCTION bce_container_of(symbol_id TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT coalesce(substring(split_part(symbol_id, '#', 1) FROM '::([^:]*)::[^:]*$'), '')
$$;

ALTER TABLE symbol_fts DROP COLUMN IF EXISTS document;

ALTER TABLE symbol_fts ADD COLUMN document tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple', coalesce(name, '')), 'A')
    || setweight(to_tsvector('simple', bce_identifier_words(coalesce(name, ''))), 'B')
    || setweight(
        to_tsvector(
            'simple',
            bce_identifier_words(bce_container_of(symbol_id)) || ' '
                || bce_identifier_words(bce_file_stem(coalesce(file_id, '')))
        ),
        'C'
    )
    || setweight(
        to_tsvector('simple', coalesce(signature, '') || ' ' || coalesce(docstring, '')), 'D'
    )
) STORED;

CREATE INDEX IF NOT EXISTS idx_symbol_fts_document ON symbol_fts USING gin (document);
