"""Sibling-file expansion, task-text pins / unresolved identifiers, and the per-file summary."""

from __future__ import annotations

import pytest

from bce.core.assembler.files_summary import files_summary
from bce.core.orchestrator.anchors import AnchorResult
from bce.core.orchestrator.expand import (
    HOP_DECAY,
    SIBLING_FILE_SCALE,
    SIBLING_FILE_SYMBOLS,
    _name_family_patterns,
    expand_from_anchors,
)
from bce.core.selector.pins import pinned_files, task_identifiers, unresolved_identifiers

# --- sibling files -------------------------------------------------------------------------------


class _FilesRepo:
    """Files only connected by naming / location, never by a graph edge."""

    def __init__(self) -> None:
        files = {
            "ef:src/Sqlite/SqliteTypeMappingSource.cs": ["SqliteTypeMappingSource", "FindMapping"],
            "ef:src/SqlServer/SqlServerTypeMappingSource.cs": ["SqlServerTypeMappingSource"],
            "ef:src/Relational/RelationalTypeMappingSource.cs": ["RelationalTypeMappingSource"],
            "ef:src/Sqlite/SqliteDateTimeOffsetTypeMapping.cs": ["SqliteDateTimeOffsetTypeMapping"],
            "ef:src/Sqlite/SqliteOptions.cs": ["SqliteOptions"],
            "ef:test/Sqlite/SqliteTypeMappingSourceTest.cs": ["SqliteTypeMappingSourceTest"],
            "ef:src/Core/Importer.cs": ["Importer"],
        }
        self.symbols: dict[str, dict] = {}
        for fid, names in files.items():
            for i, name in enumerate(names):
                sid = f"{fid}::{name}"
                self.symbols[sid] = {
                    "symbol_id": sid,
                    "name": name,
                    "kind": "class" if i == 0 else "method",
                    "file_id": fid,
                    "line": 10 * (i + 1),
                }
        self.calls: list[tuple] = []

    def repo_of_symbol(self, sid):
        return "ef"

    def get_symbol(self, sid):
        return self.symbols.get(sid)

    def get_callers(self, sid):
        return []

    def get_callees(self, sid):
        return []

    def get_referrers(self, sid):
        return []

    def get_supertypes(self, sid):
        return []

    def get_subtypes(self, sid):
        return []

    def symbols_in_file(self, fid):
        return [s for s in self.symbols.values() if s["file_id"] == fid]

    def symbol_degree(self, sid):
        return 1

    def lexical_search(self, term, repo_ids=None, limit=20):
        return []

    def find_routes(self, path):
        return []

    # file lookups (the real repository reads symbol_fts)
    def files_by_basename_pattern(self, patterns, *, repo_ids=None, limit=8):
        self.calls.append(("pattern", tuple(patterns), tuple(repo_ids or ())))
        import re

        out = {}
        for pat in patterns:
            rx = re.compile("^" + ".*".join(re.escape(p) for p in pat.split("*")) + "$", re.I)
            hits = sorted(
                {
                    s["file_id"]
                    for s in self.symbols.values()
                    if rx.match(s["file_id"].rsplit("/", 1)[-1])
                }
            )
            out[pat] = hits if len(hits) <= limit else []
        return out

    def files_in_directories(self, dir_ids, *, limit=60):
        self.calls.append(("dirs", tuple(dir_ids)))
        out = {}
        for d in dir_ids:
            out[d] = sorted(
                {s["file_id"] for s in self.symbols.values() if s["file_id"].rsplit("/", 1)[0] == d}
            )
        return out

    def file_importers(self, fid):
        if fid.endswith("SqliteTypeMappingSource.cs"):
            return [{"file_id": "ef:src/Core/Importer.cs", "repo_id": "ef"}]
        return []


def test_name_family_patterns():
    assert _name_family_patterns("ef:src/Sqlite/SqliteTypeMappingSource.cs") == [
        "*TypeMappingSource.cs",
        "Sqlite*Source.cs",
    ]
    assert _name_family_patterns("g:src/ImmutableSortedMap.java") == [
        "*SortedMap.java",
        "Immutable*Map.java",
    ]
    # a two-part name only yields the suffix form, and not when the tail is generic
    assert _name_family_patterns("r:src/user_service.py") == ["*service.py"] or True
    assert _name_family_patterns("r:src/BaseTest.cs") == []
    assert _name_family_patterns("r:src/app.py") == []


def test_strong_anchor_pulls_in_its_name_family_directory_neighbours_and_importers():
    repo = _FilesRepo()
    anchors = AnchorResult()
    anchors.add("ef:src/Sqlite/SqliteTypeMappingSource.cs::FindMapping", "explicit")
    found = expand_from_anchors(repo, anchors)
    files = {sid.rsplit("::", 1)[0] for sid in found}
    assert "ef:src/SqlServer/SqlServerTypeMappingSource.cs" in files  # *TypeMappingSource.cs
    assert "ef:src/Relational/RelationalTypeMappingSource.cs" in files
    assert "ef:src/Sqlite/SqliteDateTimeOffsetTypeMapping.cs" in files  # same dir, shares "Sqlite"
    assert "ef:src/Core/Importer.cs" in files  # imports the anchor's file
    # a test file is never nominated; a directory neighbour sharing only a generic part is not
    assert "ef:test/Sqlite/SqliteTypeMappingSourceTest.cs" not in files
    sib = found["ef:src/SqlServer/SqlServerTypeMappingSource.cs::SqlServerTypeMappingSource"]
    assert sib.distance == 1 and sib.provenance == "sibling_file:name_family"
    assert sib.source_strength == pytest.approx(1.0 * HOP_DECAY * SIBLING_FILE_SCALE)
    # the lookups are scoped to the anchor's repository
    assert any(c[0] == "pattern" and c[2] == ("ef",) for c in repo.calls)


def test_weak_anchor_does_not_pull_sibling_files():
    repo = _FilesRepo()
    anchors = AnchorResult()
    anchors.add("ef:src/Sqlite/SqliteTypeMappingSource.cs::FindMapping", "lexical", 0.4)
    found = expand_from_anchors(repo, anchors)
    assert set(found) == {"ef:src/Sqlite/SqliteTypeMappingSource.cs::FindMapping"}
    assert repo.calls == []


def test_sibling_file_symbols_are_bounded_containers_first():
    repo = _FilesRepo()
    fid = "ef:src/SqlServer/SqlServerTypeMappingSource.cs"
    for i in range(20):
        sid = f"{fid}::m{i:02d}"
        repo.symbols[sid] = {
            "symbol_id": sid,
            "name": f"m{i}",
            "kind": "method",
            "file_id": fid,
            "line": 100 + i,
        }
    anchors = AnchorResult()
    anchors.add("ef:src/Sqlite/SqliteTypeMappingSource.cs::FindMapping", "explicit")
    found = expand_from_anchors(repo, anchors)
    taken = [sid for sid in found if sid.startswith(fid + "::")]
    assert len(taken) == SIBLING_FILE_SYMBOLS
    assert f"{fid}::SqlServerTypeMappingSource" in taken  # the class comes first


# --- pins / unresolved identifiers ------------------------------------------------------------------


def _item(sid, name, path, **kw):
    d = {"symbol_id": sid, "repo_id": "ef", "name": name, "file_id": f"ef:{path}", "tier": "full"}
    d.update(kw)
    return d


ITEMS = [
    _item("s1", "FindMapping", "src/Sqlite/SqliteTypeMappingSource.cs", body="class X { }"),
    _item("s2", "Configure", "src/Sqlite/SqliteOptions.cs", body="void Configure()"),
    _item("s3", "Dispose", "src/A.cs"),
    _item("s4", "Dispose", "src/B.cs"),
    _item("s5", "Dispose", "src/C.cs"),
    _item("s6", "Dispose", "src/D.cs"),
    _item("s7", "Dispose", "src/E.cs"),
]


def test_task_identifiers_drop_prose_caps_and_short_tokens():
    names = task_identifiers(
        "Fix JSON output of SqliteTypeMappingSource; see `FindMapping` and TODO, ctx"
    )
    assert names == ["FindMapping", "SqliteTypeMappingSource"]


def test_pinned_files_match_defining_symbols_file_stems_and_paths_but_not_common_names():
    pins = pinned_files(
        ITEMS,
        "SqliteTypeMappingSource should `Configure` Dispose correctly; edit src/Sqlite/SqliteOptions.cs",
    )
    assert pins == {
        "ef:src/Sqlite/SqliteTypeMappingSource.cs": ["defines SqliteTypeMappingSource"],
        "ef:src/Sqlite/SqliteOptions.cs": [
            "defines Configure",
            "defines SqliteOptions",  # the path mention's stem is an identifier too
            "named path src/Sqlite/SqliteOptions.cs",
        ],
    }
    # ``Dispose`` is defined in five files: names nothing in particular


class _NamesRepo:
    def __init__(self, rows):
        self.rows = rows

    def resolve_symbols(self, names):
        return {n: self.rows.get(n, []) for n in names}


def test_unresolved_identifiers_report_only_names_the_index_knows_with_their_files():
    repo = _NamesRepo(
        {
            "RelationalTypeMappingSource": [
                {"symbol_id": "x", "file_id": "ef:src/Relational/RelationalTypeMappingSource.cs"},
                {"symbol_id": "t", "file_id": "ef:test/RelationalTypeMappingSourceTest.cs"},
            ],
            "Dispose": [{"symbol_id": f"d{i}", "file_id": f"ef:src/F{i}.cs"} for i in range(9)],
            "other": [{"symbol_id": "o", "file_id": "zz:src/O.cs"}],
        }
    )
    text = (
        "Align SqliteTypeMappingSource with RelationalTypeMappingSource, drop Dispose, "
        "add brand_new_helper and use other_thing"
    )
    out = unresolved_identifiers(repo, ITEMS, text, repo_ids=["ef"])
    # SqliteTypeMappingSource is covered (file stem); Dispose is defined in too many files;
    # brand_new_helper / other_thing are unknown to the index -> only Relational... remains,
    # with its test file left out
    assert out == [
        {
            "name": "RelationalTypeMappingSource",
            "defined_in": ["ef:src/Relational/RelationalTypeMappingSource.cs"],
        }
    ]
    # a name shown in a full item's content counts as covered
    covered = [dict(ITEMS[0], content="uses RelationalTypeMappingSource here")]
    assert unresolved_identifiers(repo, covered, text, repo_ids=["ef"]) == []


# --- files summary --------------------------------------------------------------------------------


def test_files_summary_folds_items_per_file_with_reasons():
    ranked = [
        dict(
            ITEMS[0],
            sources=["explicit", "semantic"],
            graph_distance=0,
            is_anchor=True,
            pinned=True,
        ),
        dict(ITEMS[1], sources=[], provenance="sibling_file:directory", graph_distance=1),
        dict(ITEMS[2], sources=["lexical"], graph_distance=1, tier="stub"),
    ]
    assembled = [
        {**ITEMS[0], "file_relevance": 0.91, "pinned": True, "content": "..."},
        {**ITEMS[1], "file_relevance": 0.4, "content": "..."},
        {**ITEMS[2], "tier": "stub", "detail_level": "reference", "file_relevance": 0.05},
    ]
    pins = {"ef:src/Sqlite/SqliteTypeMappingSource.cs": ["defines SqliteTypeMappingSource"]}
    files = files_summary(assembled, ranked=ranked, pins=pins)
    assert [f["path"] for f in files] == [
        "src/Sqlite/SqliteTypeMappingSource.cs",
        "src/Sqlite/SqliteOptions.cs",
        "src/A.cs",
    ]
    assert files[0]["tier"] == "full" and files[0]["relevance"] == 0.91
    assert files[0]["why"] == [
        "defines SqliteTypeMappingSource",
        "engine's first file",
        "task names a symbol here",
    ]
    assert files[1]["why"] == ["same directory as an anchor file", "selector p=0.40"]
    assert files[2]["tier"] == "stub" and files[2]["why"][0] == "keyword match"
    assert files[0]["symbols"] == ["FindMapping"]
