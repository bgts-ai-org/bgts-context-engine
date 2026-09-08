-- Runs once on first container init (Postgres docker entrypoint).
-- Creates the extensions so the database is ready; the application migrator (bce migrate) creates
-- the AGE graph and all relational/vector schema objects.

CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
