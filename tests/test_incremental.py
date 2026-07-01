"""Tests for git-diff based incremental re-index change detection."""

from __future__ import annotations

from cce.indexing.gitsync.local import FileChange, _parse_name_status


def test_parse_name_status_added_modified_deleted():
    raw = "A\0new.py\0M\0mod.py\0D\0gone.py\0"
    changes = _parse_name_status(raw)
    by_path = {c.path: c.status for c in changes}
    assert by_path == {"new.py": "added", "mod.py": "modified", "gone.py": "deleted"}


def test_parse_name_status_is_sorted_deterministically():
    raw = "M\0z.py\0A\0a.py\0D\0m.py\0"
    changes = _parse_name_status(raw)
    assert [c.path for c in changes] == ["a.py", "m.py", "z.py"]


def test_parse_name_status_normalizes_backslashes():
    raw = "A\0dir\\sub\\file.py\0"
    changes = _parse_name_status(raw)
    assert changes == [FileChange(status="added", path="dir/sub/file.py")]


def test_parse_name_status_unknown_status_treated_as_modified():
    raw = "T\0typechange.py\0"
    changes = _parse_name_status(raw)
    assert changes == [FileChange(status="modified", path="typechange.py")]


def test_parse_name_status_ignores_dangling_status():
    raw = "A\0new.py\0M\0"
    changes = _parse_name_status(raw)
    assert changes == [FileChange(status="added", path="new.py")]
