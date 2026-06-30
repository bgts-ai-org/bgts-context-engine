"""CCE command-line interface (Phase 0).

Commands:
- ``migrate``          apply database migrations
- ``index``            full-index a local repository
- ``index-remote``     clone a Bitbucket repo by URL and full-index it
- ``resolve-symbol``   Layer-1: resolve a symbol by name
- ``find-references``  Layer-1: find references to a symbol_id
- ``languages``        list supported languages/extensions (no database needed)
- ``serve``            run the REST API (FastAPI/uvicorn)

Read/write commands need a running PostgreSQL (Apache AGE + pgvector); ``languages`` does not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, help="Cortex Context Engine CLI")


def _echo_json(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


@app.command()
def migrate() -> None:
    """Apply pending database migrations."""
    from cce.storage.relational.migrator import run_migrations

    applied = run_migrations()
    if applied:
        typer.echo("Applied migrations: " + ", ".join(applied))
    else:
        typer.echo("No pending migrations.")


@app.command()
def index(
    repo: Path = typer.Option(..., "--repo", help="Path to a local repository to index"),
    name: str = typer.Option(..., "--name", help="Logical repository name"),
    commit: str | None = typer.Option(None, "--commit", help="Override commit sha"),
    locale: str | None = typer.Option(None, "--locale", help="Message locale (en/tr)"),
) -> None:
    """Full-index a local repository into the code graph."""
    from cce.core.i18n import get_translator
    from cce.indexing.indexer import Indexer
    from cce.storage.graph.client import GraphClient
    from cce.storage.graph.repository import GraphRepository
    from cce.storage.relational.db import connection

    with connection() as conn:
        repository = GraphRepository(GraphClient(conn))
        summary = Indexer(repository).index_local_repo(path=repo, name=name, commit=commit)
        conn.commit()

    tr = get_translator()
    loc = tr.resolve(locale)
    typer.echo(
        tr.translate(
            "tool.indexing.done",
            loc,
            files=summary.files,
            nodes=summary.nodes,
            edges=summary.edges,
            commit=summary.commit,
        )
    )
    _echo_json(
        {
            "repo_id": summary.repo_id,
            "files": summary.files,
            "nodes": summary.nodes,
            "edges": summary.edges,
            "commit": summary.commit,
        }
    )


@app.command(name="index-remote")
def index_remote_cmd(
    url: str = typer.Option(..., "--url", help="Bitbucket repository URL (https or ssh form)"),
    name: str | None = typer.Option(None, "--name", help="Logical name (default: workspace/repo)"),
    branch: str | None = typer.Option(None, "--branch", help="Branch to index (default: remote HEAD)"),
    token: str | None = typer.Option(
        None, "--token", help="Access token / app password (else env CCE_BITBUCKET_TOKEN)"
    ),
    username: str | None = typer.Option(
        None, "--username", help="Username for app password (else env CCE_BITBUCKET_USERNAME)"
    ),
    locale: str | None = typer.Option(None, "--locale", help="Message locale (en/tr)"),
) -> None:
    """Clone a remote repository (e.g. Bitbucket Cloud) and full-index it."""
    from cce.config import get_settings
    from cce.core.i18n import get_translator
    from cce.indexing.gitsync import GitCredentials, GitError
    from cce.indexing.indexer import Indexer
    from cce.storage.graph.client import GraphClient
    from cce.storage.graph.repository import GraphRepository
    from cce.storage.relational.db import connection

    settings = get_settings()
    creds = GitCredentials(
        username=username if username is not None else settings.bitbucket_username,
        token=token if token is not None else settings.bitbucket_token,
    )
    tr = get_translator()
    loc = tr.resolve(locale)

    try:
        with connection() as conn:
            repository = GraphRepository(GraphClient(conn))
            summary = Indexer(repository).index_remote_repo(
                url=url, name=name, branch=branch, credentials=creds
            )
            conn.commit()
    except ValueError as exc:
        typer.echo(tr.translate("error.invalid_repo_url", loc, detail=str(exc)), err=True)
        raise typer.Exit(code=2) from exc
    except GitError as exc:
        typer.echo(tr.translate("error.remote_index_failed", loc, url=url, detail=str(exc)), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        tr.translate(
            "tool.indexing.done",
            loc,
            files=summary.files,
            nodes=summary.nodes,
            edges=summary.edges,
            commit=summary.commit,
        )
    )
    _echo_json(
        {
            "repo_id": summary.repo_id,
            "name": name or summary.repo_id,
            "remote_url": summary.remote_url,
            "branch": summary.branch,
            "files": summary.files,
            "nodes": summary.nodes,
            "edges": summary.edges,
            "commit": summary.commit,
        }
    )


@app.command(name="resolve-symbol")
def resolve_symbol_cmd(
    name: str = typer.Option(..., "--name", help="Symbol name to resolve"),
    repo: str | None = typer.Option(None, "--repo", help="Restrict to a repo_id"),
    locale: str | None = typer.Option(None, "--locale", help="Message locale (en/tr)"),
) -> None:
    """Resolve a symbol by name (Layer 1)."""
    from cce.storage.graph.client import GraphClient
    from cce.storage.graph.repository import GraphRepository
    from cce.storage.relational.db import connection
    from cce.tools.layer1 import resolve_symbol

    with connection() as conn:
        repository = GraphRepository(GraphClient(conn))
        result = resolve_symbol(repository, name, repo_id=repo, locale=locale)
    if result["message"]:
        typer.echo(result["message"])
    _echo_json(result["payload"])


@app.command(name="find-references")
def find_references_cmd(
    symbol_id: str = typer.Option(..., "--symbol-id", help="symbol_id to find references for"),
    locale: str | None = typer.Option(None, "--locale", help="Message locale (en/tr)"),
) -> None:
    """Find references to a symbol (Layer 1)."""
    from cce.storage.graph.client import GraphClient
    from cce.storage.graph.repository import GraphRepository
    from cce.storage.relational.db import connection
    from cce.tools.layer1 import find_references

    with connection() as conn:
        repository = GraphRepository(GraphClient(conn))
        result = find_references(repository, symbol_id, locale=locale)
    typer.echo(result["message"])
    _echo_json(result["payload"])


@app.command()
def languages() -> None:
    """List supported languages and file extensions (no database needed)."""
    from cce.indexing.parser.registry import build_default_registry

    registry = build_default_registry()
    _echo_json(
        {
            "languages": list(registry.languages()),
            "extensions": list(registry.supported_extensions()),
        }
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host"),
    port: int = typer.Option(8000, "--port", help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev)"),
) -> None:
    """Run the REST API server (Layer-1 endpoints) via uvicorn."""
    import uvicorn

    uvicorn.run("cce.api.rest.app:app", host=host, port=port, reload=reload)


def main() -> None:
    # Ensure localized (e.g. Turkish) output works on consoles defaulting to cp1252.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    app()


if __name__ == "__main__":
    main()
