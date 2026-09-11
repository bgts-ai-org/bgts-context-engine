"""BCE command-line interface (Phase 0).

Commands:
- ``migrate``          apply database migrations
- ``index``            full-index a local repository
- ``index-remote``     clone a Bitbucket repo by URL and full-index it
- ``reindex``          incrementally re-index changed files since the last commit (git-diff)
- ``bench``            run the POC benchmark (latency/recall/precision/determinism/RLS) -> JSON
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

app = typer.Typer(add_completion=False, help="BGTS Context Engine CLI")


def _echo_json(data: object) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _print_version(value: bool) -> None:
    if value:
        from bce import __version__

        typer.echo(f"bce {__version__}")
        raise typer.Exit()


_ENV_FILE_HELP = "Path to a .env file to load (default: ./.env; also settable via BCE_ENV_FILE)"


def _load_env_file(env_file: Path | None) -> None:
    """Point configuration at ``env_file``, rejecting a path that does not exist."""
    if env_file is None:
        return
    from bce.config import set_env_file

    if not env_file.is_file():
        raise typer.BadParameter(f"env file not found: {env_file}", param_hint="--env-file")
    set_env_file(str(env_file.resolve()))


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the engine version and exit",
        callback=_print_version,
        is_eager=True,
    ),
    env_file: Path | None = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
) -> None:
    """BGTS Context Engine: deterministic code-graph context for AI coding agents."""
    _load_env_file(env_file)


@app.command()
def migrate() -> None:
    """Apply pending database migrations."""
    from bce.storage.relational.migrator import run_migrations

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
    from bce.core.i18n import get_translator
    from bce.indexing.indexer import Indexer
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection

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
            "nodes_added": summary.nodes_added,
            "edges_added": summary.edges_added,
            "commit": summary.commit,
        }
    )


@app.command(name="index-remote")
def index_remote_cmd(
    url: str = typer.Option(..., "--url", help="Bitbucket repository URL (https or ssh form)"),
    name: str | None = typer.Option(None, "--name", help="Logical name (default: workspace/repo)"),
    branch: str | None = typer.Option(
        None, "--branch", help="Branch to index (default: remote HEAD)"
    ),
    token: str | None = typer.Option(
        None, "--token", help="Access token / app password (else env BCE_BITBUCKET_TOKEN)"
    ),
    username: str | None = typer.Option(
        None, "--username", help="Username for app password (else env BCE_BITBUCKET_USERNAME)"
    ),
    locale: str | None = typer.Option(None, "--locale", help="Message locale (en/tr)"),
) -> None:
    """Clone a remote repository (e.g. Bitbucket Cloud) and full-index it."""
    from bce.config import get_settings
    from bce.core.i18n import get_translator
    from bce.indexing.gitsync import GitCredentials, GitError
    from bce.indexing.indexer import Indexer
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection

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
        typer.echo(
            tr.translate("error.remote_index_failed", loc, url=url, detail=str(exc)), err=True
        )
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
            "nodes_added": summary.nodes_added,
            "edges_added": summary.edges_added,
            "commit": summary.commit,
        }
    )


@app.command()
def reindex(
    repo: Path = typer.Option(
        ..., "--repo", help="Path to the local repository (git working tree)"
    ),
    name: str = typer.Option(
        ..., "--name", help="Logical repository name (must match prior index)"
    ),
    since: str | None = typer.Option(
        None, "--since", help="Baseline commit (default: repo's last_indexed_commit)"
    ),
    to: str = typer.Option("HEAD", "--to", help="Target commit/ref (default: HEAD)"),
    locale: str | None = typer.Option(None, "--locale", help="Message locale (en/tr)"),
) -> None:
    """Incrementally re-index only the files changed since the last index (git-diff)."""
    from bce.core.i18n import get_translator
    from bce.indexing.indexer import Indexer
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection

    tr = get_translator()
    loc = tr.resolve(locale)
    try:
        with connection() as conn:
            repository = GraphRepository(GraphClient(conn))
            summary = Indexer(repository).index_incremental(
                path=repo, name=name, since_commit=since, to_commit=to
            )
            conn.commit()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(
        tr.translate(
            "tool.reindex.done",
            loc,
            added=summary.added,
            modified=summary.modified,
            deleted=summary.deleted,
            commit=summary.to_commit,
        )
    )
    _echo_json(
        {
            "repo_id": summary.repo_id,
            "from_commit": summary.from_commit,
            "to_commit": summary.to_commit,
            "added": summary.added,
            "modified": summary.modified,
            "deleted": summary.deleted,
            "nodes": summary.nodes,
            "edges": summary.edges,
            "nodes_added": summary.nodes_added,
            "edges_added": summary.edges_added,
        }
    )


@app.command(name="resolve-symbol")
def resolve_symbol_cmd(
    name: str = typer.Option(..., "--name", help="Symbol name to resolve"),
    repo: str | None = typer.Option(None, "--repo", help="Restrict to a repo_id"),
    locale: str | None = typer.Option(None, "--locale", help="Message locale (en/tr)"),
) -> None:
    """Resolve a symbol by name (Layer 1)."""
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection
    from bce.tools.layer1 import resolve_symbol

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
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection
    from bce.tools.layer1 import find_references

    with connection() as conn:
        repository = GraphRepository(GraphClient(conn))
        result = find_references(repository, symbol_id, locale=locale)
    typer.echo(result["message"])
    _echo_json(result["payload"])


@app.command()
def context(
    task: str = typer.Option(..., "--task", help="Task title + description text"),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Token budget for assembly"),
    max_candidates: int = typer.Option(
        8, "--max-candidates", help="Narrow to at most N candidates"
    ),
    commit: str | None = typer.Option(None, "--commit", help="Pinned commit sha (stage 0)"),
    locale: str | None = typer.Option(None, "--locale", help="Message locale (en/tr)"),
) -> None:
    """Layer 3: assemble a deterministic context package + coverage for a task."""
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection
    from bce.storage.vector.store import VectorStore
    from bce.tools.layer3 import get_context_for_task

    with connection() as conn:
        repository = GraphRepository(GraphClient(conn))
        result = get_context_for_task(
            repository,
            task_text=task,
            max_tokens=max_tokens,
            max_candidates=max_candidates,
            commit=commit,
            store=VectorStore(conn),
            locale=locale,
        )
    if result["message"]:
        typer.echo(result["message"])
    _echo_json(result["payload"])


@app.command()
def bench(
    cases: Path = typer.Option(..., "--cases", help="Path to a JSON file of benchmark cases"),
    out: Path | None = typer.Option(None, "--out", help="Write the JSON report here (else stdout)"),
    determinism_runs: int = typer.Option(
        3, "--determinism-runs", help="Repeats for the determinism check"
    ),
) -> None:
    """POC benchmark: run a task set and report latency/recall/precision/determinism/RLS as JSON."""
    from bce.bench.runner import load_cases, run_benchmark
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection

    bench_cases = load_cases(str(cases))
    with connection() as conn:
        repository = GraphRepository(GraphClient(conn))
        report = run_benchmark(repository, bench_cases, determinism_runs=determinism_runs)

    data = report.to_dict()
    if out is not None:
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        typer.echo(f"Wrote benchmark report to {out}")
    _echo_json(data)


@app.command()
def languages() -> None:
    """List supported languages and file extensions (no database needed)."""
    from bce.indexing.parser.registry import build_default_registry

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
    ui: bool = typer.Option(True, "--ui/--no-ui", help="Serve the bundled web UI at /ui"),
    env_file: Path | None = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
) -> None:
    """Run the REST API server (Layer 1-2-3 endpoints) via uvicorn.

    ``--env-file`` is accepted here as well as before the command name: like ``serve-mcp``, this
    command is launched by a supervisor or service definition that reads more naturally with the
    option after it.
    """
    import uvicorn

    from bce.api.rest.app import ui_is_bundled
    from bce.core.logging import setup_logging

    _load_env_file(env_file)
    setup_logging()

    typer.echo(f"API docs:  http://{host}:{port}/docs")
    if ui and ui_is_bundled():
        typer.echo(f"Web UI:    http://{host}:{port}/ui/")
    elif ui:
        typer.echo("Web UI:    not bundled in this installation (see web/README.md to run it)")

    factory = "bce.api.rest.app:app" if ui else "bce.api.rest.app:create_api_only_app"
    uvicorn.run(factory, host=host, port=port, reload=reload, factory=not ui)


@app.command(name="serve-mcp")
def serve_mcp(
    env_file: Path | None = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
) -> None:
    """Run the MCP server over stdio (agent-native surface). Requires the 'mcp' package.

    ``--env-file`` is accepted here as well as before the command name, because an editor's MCP
    config spawns this command directly and reads more naturally with the option after it.
    """
    import asyncio

    _load_env_file(env_file)

    from bce.api.mcp.server import run_stdio

    asyncio.run(run_stdio())


def main() -> None:
    # Ensure localized (e.g. Turkish) output works on consoles defaulting to cp1252.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    from bce.indexing.embedder.encoder import EncoderConfigError

    try:
        app()
    except EncoderConfigError as exc:
        # A misconfigured embedding provider is the operator's to fix and the message says how, so
        # catch it before Typer's excepthook buries it in a traceback. typer.Exit is a RuntimeError
        # that only Click's own invocation understands; outside app() it must be SystemExit.
        typer.secho(f"Configuration error: {exc}", err=True, fg=typer.colors.RED)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
