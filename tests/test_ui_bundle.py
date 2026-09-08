"""Serving of the compiled frontend under /ui - no database, no Node build required.

A released distribution vendors the Vite bundle into ``bce/api/rest/static``. A source
checkout has no bundle, so these tests fabricate one in a temporary directory and point
``UI_DIST_DIR`` at it.
"""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# bce.api.rest re-exports the FastAPI instance under the name "app", which shadows the
# submodule of the same name. Resolve the module explicitly so UI_DIST_DIR can be patched.
app_module = importlib.import_module("bce.api.rest.app")

_INDEX_HTML = "<!doctype html><html><body><div id='root'></div></body></html>"
_ASSET_JS = "console.info('bundle');"


@pytest.fixture
def bundled_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Fake a vendored frontend bundle on disk."""
    dist = tmp_path / "static"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "main.js").write_text(_ASSET_JS, encoding="utf-8")
    monkeypatch.setattr(app_module, "UI_DIST_DIR", dist)
    yield dist


@pytest.fixture
def unbundled_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point at an empty directory, matching a source checkout that never ran the build.

    Patched rather than relying on the real path, which does hold a bundle on any machine
    that has run scripts/build_ui.py.
    """
    missing = tmp_path / "no-bundle"
    monkeypatch.setattr(app_module, "UI_DIST_DIR", missing)
    yield missing


def test_ui_is_not_bundled_without_a_build(unbundled_ui: Path) -> None:
    assert not app_module.ui_is_bundled()


def test_ui_routes_absent_without_a_bundle(unbundled_ui: Path) -> None:
    client = TestClient(app_module.create_app())
    assert client.get("/ui/", follow_redirects=False).status_code == 404
    # The API itself stays fully functional without the UI.
    assert client.get("/v1/languages").status_code == 200


def test_ui_bundle_is_never_tracked_by_git() -> None:
    """The compiled bundle is a build artifact and must stay out of version control."""
    tracked = subprocess.run(
        ["git", "ls-files", "src/bce/api/rest/static"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    assert tracked.stdout.strip() == ""


def test_ui_serves_index_and_assets(bundled_ui: Path) -> None:
    client = TestClient(app_module.create_app())

    index = client.get("/ui/")
    assert index.status_code == 200
    assert "<div id='root'></div>" in index.text

    asset = client.get("/ui/assets/main.js")
    assert asset.status_code == 200
    assert asset.text == _ASSET_JS


def test_ui_bare_path_redirects_to_trailing_slash(bundled_ui: Path) -> None:
    resp = TestClient(app_module.create_app()).get("/ui", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/ui/"


def test_ui_missing_asset_returns_404(bundled_ui: Path) -> None:
    """No catch-all fallback: a missing asset must not be masked by an HTML response."""
    resp = TestClient(app_module.create_app()).get("/ui/assets/does-not-exist.js")
    assert resp.status_code == 404


def test_serve_ui_false_skips_the_mount(bundled_ui: Path) -> None:
    client = TestClient(app_module.create_app(serve_ui=False))
    assert client.get("/ui/", follow_redirects=False).status_code == 404
    assert client.get("/v1/languages").status_code == 200


def test_ui_mount_stays_out_of_the_openapi_schema(bundled_ui: Path) -> None:
    paths = set(app_module.create_app().openapi()["paths"])
    assert not any(p.startswith("/ui") for p in paths)
