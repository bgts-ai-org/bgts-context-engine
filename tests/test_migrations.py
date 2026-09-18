"""Static checks on the SQL migrations (no database needed)."""

from __future__ import annotations

from bce.domain.enums import NodeLabel
from bce.storage.relational.migrator import MIGRATIONS_DIR, split_sql_statements


def test_migration_ids_are_unique_and_sequential() -> None:
    ids = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
    numbers = [int(i.split("_", 1)[0]) for i in ids]
    assert numbers == list(range(1, len(ids) + 1)), ids


def test_graph_label_index_migration_splits_into_three_statements() -> None:
    sql = (MIGRATIONS_DIR / "0008_graph_label_indexes.sql").read_text(encoding="utf-8-sig")
    statements = split_sql_statements(sql)
    # LOAD 'age'; SET LOCAL search_path; one DO block. The splitter must keep the DO body whole.
    assert len(statements) == 3, [s[:40] for s in statements]
    assert statements[-1].rstrip().endswith("$$")
    assert statements[-1].count("$$") == 2


def test_graph_label_index_migration_covers_every_vertex_label() -> None:
    sql = (MIGRATIONS_DIR / "0008_graph_label_indexes.sql").read_text(encoding="utf-8-sig")
    for label in NodeLabel:
        assert f"'{label.value}'" in sql, f"{label.value} missing from label array"
    # Same shape as the hand-made indexes on the already-indexed databases: GIN on properties
    # with the pending list disabled, named <label>_properties_gin.
    assert "USING gin (properties) WITH (fastupdate = off)" in sql
    assert "|| '_properties_gin'" in sql
    assert "create_vlabel('code_graph'::cstring, label::cstring)" in sql


def test_fts_identifier_parts_migration_splits_and_rebuilds_the_document() -> None:
    sql = (MIGRATIONS_DIR / "0009_fts_identifier_parts.sql").read_text(encoding="utf-8-sig")
    statements = split_sql_statements(sql)
    # 3 helper functions (dollar-quoted bodies kept whole), DROP COLUMN, ADD COLUMN, CREATE INDEX.
    assert len(statements) == 6, [s[:50] for s in statements]
    # The header comment travels with the first statement, hence "in" rather than startswith.
    functions = [s for s in statements if "CREATE OR REPLACE FUNCTION" in s]
    assert len(functions) == 3
    # A generated column may only call IMMUTABLE functions.
    assert all("IMMUTABLE" in f and f.count("$$") == 2 for f in functions)
    assert "DROP COLUMN IF EXISTS document" in statements[3]
    assert "GENERATED ALWAYS AS" in statements[4]
    # Weighted parts: exact name > name parts > container/file parts > signature/docstring.
    for weight in ("'A'", "'B'", "'C'", "'D'"):
        assert weight in statements[4]
    assert "bce_container_of(symbol_id)" in statements[4]
    assert "bce_file_stem(coalesce(file_id, ''))" in statements[4]
    assert statements[5].startswith("CREATE INDEX IF NOT EXISTS idx_symbol_fts_document")
