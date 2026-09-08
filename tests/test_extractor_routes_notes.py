"""Golden tests for route (feature 1) and design-note (feature 5) extraction."""

from __future__ import annotations

from bce.domain.enums import DesignNoteKind, EdgeLabel, NodeLabel, Provenance
from bce.indexing.extractor import Extractor

FASTAPI_SOURCE = b'''"""API module."""
from fastapi import FastAPI

app = FastAPI()


# WHY: idempotent so retries are safe
@app.get("/users/{id}")
def get_user(id):
    return id


@app.post("/users")
def create_user(body):
    # TODO: validate the payload
    return body
'''

FLASK_SOURCE = b'''from flask import Flask

app = Flask(__name__)


@app.route("/legacy", methods=["POST", "PUT"])
def legacy():
    return 1
'''

EXPRESS_SOURCE = b'''const app = express();

app.get("/health", (req, res) => res.send("ok"));
app.post("/items", createItem);
'''


def _routes(frag):
    return [n for n in frag.nodes if n.label is NodeLabel.ROUTE]


def _notes(frag):
    return [n for n in frag.nodes if n.label is NodeLabel.DESIGN_NOTE]


def test_fastapi_routes_extracted():
    frag = Extractor().extract_file(repo_id="demo", path="api.py", source=FASTAPI_SOURCE)
    routes = {(r.properties["http_method"], r.properties["path_pattern"]) for r in _routes(frag)}
    assert ("GET", "/users/{id}") in routes
    assert ("POST", "/users") in routes
    for r in _routes(frag):
        assert r.properties["framework"] == "fastapi"


def test_routes_link_to_handler_with_heuristic_provenance():
    frag = Extractor().extract_file(repo_id="demo", path="api.py", source=FASTAPI_SOURCE)
    routes_to = [e for e in frag.edges if e.label is EdgeLabel.ROUTES_TO]
    assert routes_to, "expected ROUTES_TO edges"
    for edge in routes_to:
        assert edge.provenance is Provenance.HEURISTIC
        assert edge.synthesized_by == "route-binding"


def test_flask_route_expands_methods():
    frag = Extractor().extract_file(repo_id="demo", path="app.py", source=FLASK_SOURCE)
    methods = {r.properties["http_method"] for r in _routes(frag)}
    assert methods == {"POST", "PUT"}
    for r in _routes(frag):
        assert r.properties["framework"] == "flask"


def test_express_routes_extracted():
    frag = Extractor().extract_file(repo_id="demo", path="server.js", source=EXPRESS_SOURCE)
    routes = {(r.properties["http_method"], r.properties["path_pattern"]) for r in _routes(frag)}
    assert ("GET", "/health") in routes
    assert ("POST", "/items") in routes


def test_design_notes_classified_and_linked():
    frag = Extractor().extract_file(repo_id="demo", path="api.py", source=FASTAPI_SOURCE)
    notes = _notes(frag)
    kinds = {n.properties["kind"] for n in notes}
    assert str(DesignNoteKind.WHY) in kinds
    assert str(DesignNoteKind.TODO) in kinds

    explains = [e for e in frag.edges if e.label is EdgeLabel.EXPLAINS]
    assert explains, "expected EXPLAINS edges"
    for edge in explains:
        assert edge.provenance is Provenance.TREESITTER


def test_extraction_with_routes_and_notes_is_deterministic():
    a = Extractor().extract_file(repo_id="demo", path="api.py", source=FASTAPI_SOURCE)
    b = Extractor().extract_file(repo_id="demo", path="api.py", source=FASTAPI_SOURCE)
    assert [n.dedup_key for n in a.nodes] == [n.dedup_key for n in b.nodes]
    assert [e.dedup_key for e in a.edges] == [e.dedup_key for e in b.edges]
