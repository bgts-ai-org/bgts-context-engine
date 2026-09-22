"""BCE command-line interface (Phase 0).

Commands:
- ``migrate``          apply database migrations
- ``index``            full-index a local repository
- ``index-remote``     clone a Bitbucket repo by URL and full-index it
- ``reindex``          incrementally re-index changed files since the last commit (git-diff)
- ``bench``            run the POC benchmark (latency/recall/precision/determinism/RLS) -> JSON
- ``bench-prs``        PR replay ablation: Voyage alone vs Voyage + BCE (``prepare`` / ``run``)
- ``resolve-symbol``   Layer-1: resolve a symbol by name
- ``find-references``  Layer-1: find references to a symbol_id
- ``context``          Layer-3: assemble the context package for a task
- ``precontext``       the compact context block an agent prompt carries (also a Claude Code hook)
- ``cursor-init``      write .cursor/mcp.json + the agent rule into a project
- ``claude-init``      write .mcp.json, a CLAUDE.md section and the pre-context hook into a project
- ``languages``        list supported languages/extensions (no database needed)
- ``serve``            run the REST API (FastAPI/uvicorn)
- ``serve-mcp``        run the MCP server over stdio

Read/write commands need a running PostgreSQL (Apache AGE + pgvector); ``languages``,
``cursor-init`` and ``claude-init`` do not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from bce.core.defaults import DEFAULT_MAX_CANDIDATES, DEFAULT_MAX_TOKENS

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
def migrate(
    reset_embeddings: bool = typer.Option(
        False,
        "--reset-embeddings",
        help=(
            "Allow re-typing embeddings.embedding to BCE_EMBEDDING_DIM even when it holds vectors "
            "of another width (they are discarded; reindex afterwards)"
        ),
    ),
) -> None:
    """Apply pending database migrations and fit the vector column to BCE_EMBEDDING_DIM."""
    from bce.config import get_settings
    from bce.storage.relational.db import connection
    from bce.storage.relational.migrator import (
        EmbeddingDimMismatch,
        align_embedding_dim,
        run_migrations,
    )

    dim = get_settings().embedding_dim
    with connection() as conn:
        applied = run_migrations(conn)
        try:
            resized = align_embedding_dim(conn, dim, reset=reset_embeddings)
        except EmbeddingDimMismatch as exc:
            typer.echo(f"Configuration error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    if applied:
        typer.echo("Applied migrations: " + ", ".join(applied))
    else:
        typer.echo("No pending migrations.")
    if resized:
        typer.echo(f"Re-typed embeddings.embedding to vector({dim}).")


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


@app.command()
def churn(
    repo: Path = typer.Option(..., "--repo", help="Path to the local git repository"),
    name: str = typer.Option(..., "--name", help="Logical repository name (as indexed)"),
    commit: str | None = typer.Option(
        None, "--commit", help="Commit the window ends at (default: the repo's indexed commit)"
    ),
    db_name: str | None = typer.Option(
        None, "--db-name", help="Database to write to (default: the configured one)"
    ),
) -> None:
    """(Re)compute per-file git churn for an indexed repository (scoring prior, migration 0010).

    The indexer records churn on every run; this backfills databases indexed before the
    migration or refreshes the window without re-indexing. Deterministic given the commit.
    """
    from bce.config import get_settings
    from bce.indexing.churn import (
        CHURN_WINDOW_COMMITS,
        churn_available,
        file_churn,
        indexed_paths,
        write_file_churn,
    )
    from bce.indexing.parser.symbol_id import make_repo_id
    from bce.storage.relational.db import connection

    settings = get_settings()
    if db_name:
        settings = settings.model_copy(update={"db_name": db_name})
    repo_id = make_repo_id(name)
    with connection(settings) as conn:
        if not churn_available(conn):
            typer.echo("file_churn table missing: run `bce migrate` first", err=True)
            raise typer.Exit(code=2)
        target = commit
        if not target:
            with conn.cursor() as cur:
                cur.execute("SELECT last_indexed_commit FROM repos WHERE repo_id = %s", (repo_id,))
                row = cur.fetchone()
            target = row[0] if row and row[0] else "HEAD"
        counts = file_churn(repo, commit=target)
        paths = indexed_paths(conn, repo_id)
        rows = write_file_churn(
            conn, repo_id=repo_id, counts=counts, commit=target, only_paths=paths
        )
        conn.commit()
    _echo_json(
        {
            "repo_id": repo_id,
            "commit": target,
            "window_commits": CHURN_WINDOW_COMMITS,
            "files_indexed": len(paths),
            "files_with_history": sum(1 for p in paths if counts.get(p)),
            "rows": rows,
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
    max_tokens: int = typer.Option(
        DEFAULT_MAX_TOKENS, "--max-tokens", help="Token budget for assembly"
    ),
    max_candidates: int = typer.Option(
        DEFAULT_MAX_CANDIDATES, "--max-candidates", help="Narrow to at most N candidates (K)"
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
def precontext(
    task: str | None = typer.Option(
        None,
        "--task",
        help="Task text. Omit it to read a Claude Code UserPromptSubmit hook payload from stdin.",
    ),
    repo_id: list[str] = typer.Option(
        [], "--repo-id", help="Restrict to these repository ids (repeatable)"
    ),
    max_candidates: int = typer.Option(DEFAULT_MAX_CANDIDATES, "--max-candidates", help="K"),
    max_tokens: int = typer.Option(DEFAULT_MAX_TOKENS, "--max-tokens", help="Token budget"),
) -> None:
    """The compact "Code context for this task" block an agent starts with (markdown).

    With ``--task`` it prints the block. Without it, it acts as a Claude Code ``UserPromptSubmit``
    hook: reads the hook JSON on stdin and prints the ``additionalContext`` response, or nothing
    when the prompt is not a task. It never fails the prompt: errors go to stderr, exit code 0.
    """
    from bce.integrations.precontext import claude_hook_response, precontext_for_task

    if task is not None:
        pre = precontext_for_task(
            task, repo_ids=repo_id or None, max_candidates=max_candidates, max_tokens=max_tokens
        )
        typer.echo(pre["text"], nl=False)
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        typer.echo(f"bce precontext: stdin is not JSON ({exc})", err=True)
        return
    response = claude_hook_response(payload, repo_ids=repo_id or None)
    if response is not None:
        typer.echo(json.dumps(response, ensure_ascii=False))


def _agent_init_common(
    project: Path, repo_id: list[str], env_file: Path | None, bce_command: str | None
) -> tuple[Path, list[str], str, Path | None]:
    from bce.integrations.agents import resolve_bce_command, resolve_env_file

    project = project.resolve()
    if not project.is_dir():
        raise typer.BadParameter(f"not a directory: {project}", param_hint="--project")
    repo_ids = [r.strip() for r in repo_id if r.strip()] or [project.name]
    try:
        resolved_env = resolve_env_file(env_file, project)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--env-file") from None
    return project, repo_ids, resolve_bce_command(bce_command), resolved_env


def _report_setup(result, env_file: Path | None) -> None:
    for p in result.written:
        typer.echo(f"wrote {p}")
    if env_file is None:
        typer.secho(
            "No .env found: the MCP server will read BCE_* from the editor's environment. Pass "
            "--env-file PATH (or run `bce --env-file PATH ...`) to point it at your engine "
            "configuration.",
            err=True,
            fg=typer.colors.YELLOW,
        )
    for n in result.notes:
        typer.echo(f"- {n}")


_INIT_PROJECT_HELP = "Project directory to configure (default: current directory)"
_INIT_REPO_HELP = "Repository id as indexed in the engine (repeatable; default: the directory name)"
_INIT_ENV_HELP = "The engine .env the MCP server loads (default: --env-file/BCE_ENV_FILE, then <project>/.env, then ./.env)"
_INIT_CMD_HELP = "Executable the editor spawns (default: this bce)"


@app.command(name="cursor-init")
def cursor_init(
    project: Path = typer.Option(Path("."), "--project", help=_INIT_PROJECT_HELP),
    repo_id: list[str] = typer.Option([], "--repo-id", help=_INIT_REPO_HELP),
    env_file: Path | None = typer.Option(None, "--env-file", help=_INIT_ENV_HELP),
    bce_command: str | None = typer.Option(None, "--bce-command", help=_INIT_CMD_HELP),
) -> None:
    """Connect a project to the engine for Cursor: .cursor/mcp.json + the agent rule.

    Merges into an existing mcp.json (other servers are kept) and is safe to rerun.
    """
    from bce.integrations.agents import setup_cursor

    project, repo_ids, cmd, env = _agent_init_common(project, repo_id, env_file, bce_command)
    _report_setup(setup_cursor(project, repo_ids=repo_ids, bce_cmd=cmd, env_file=env), env)


@app.command(name="claude-init")
def claude_init(
    project: Path = typer.Option(Path("."), "--project", help=_INIT_PROJECT_HELP),
    repo_id: list[str] = typer.Option([], "--repo-id", help=_INIT_REPO_HELP),
    env_file: Path | None = typer.Option(None, "--env-file", help=_INIT_ENV_HELP),
    bce_command: str | None = typer.Option(None, "--bce-command", help=_INIT_CMD_HELP),
    hook: bool = typer.Option(
        True,
        "--hook/--no-hook",
        help="Install the UserPromptSubmit hook that adds the graph's answer to every prompt",
    ),
) -> None:
    """Connect a project to the engine for Claude Code: .mcp.json, a CLAUDE.md section and, by
    default, the pre-computed context hook in .claude/settings.json.

    Merges into existing files (other servers, hooks and CLAUDE.md content are kept) and is safe
    to rerun.
    """
    from bce.integrations.agents import setup_claude

    project, repo_ids, cmd, env = _agent_init_common(project, repo_id, env_file, bce_command)
    _report_setup(
        setup_claude(project, repo_ids=repo_ids, bce_cmd=cmd, env_file=env, hook=hook), env
    )


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


bench_prs_app = typer.Typer(
    add_completion=False,
    help=(
        "PR replay ablation: Voyage embeddings alone vs Voyage + BCE on real merged PRs. "
        "Each PR needs a database (default name bce_pr<N>) indexing the repo at the PR's base commit."
    ),
)
app.add_typer(bench_prs_app, name="bench-prs")


def _parse_numbers(raw: str) -> list[int]:
    return [int(part) for part in raw.replace(";", ",").split(",") if part.strip()]


_TUNE_HELP = (
    "PR numbers the engine's constants may be fitted on; the rest are the holdout the report "
    "scores separately"
)


@bench_prs_app.command("prepare")
def bench_prs_prepare(
    prs: str = typer.Option(..., "--prs", help="Comma-separated PR numbers, e.g. 829,831,833"),
    repo: Path = typer.Option(
        ..., "--repo", help="Local git clone holding the PRs' source/destination commits"
    ),
    out: Path = typer.Option(..., "--out", help="Where to write cases.json"),
    workspace: str = typer.Option(..., "--workspace", help="Bitbucket workspace"),
    repo_slug: str = typer.Option(..., "--repo-slug", help="Bitbucket repository slug"),
    unscored: str = typer.Option(
        "", "--unscored", help="PR numbers to report but leave out of the aggregate"
    ),
    tune: str = typer.Option("", "--tune", help=_TUNE_HELP),
    db_prefix: str = typer.Option("bce_pr", "--db-prefix", help="Per-PR database name prefix"),
) -> None:
    """Fetch PR metadata, map each diff onto its snapshot and write the ground-truth cases."""
    from bce.bench.pr_ablation import (
        BenchPRCase,
        build_ground_truth,
        fetch_pr_specs,
        save_cases,
        task_variants,
    )
    from bce.config import get_settings
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection

    settings = get_settings()
    numbers = _parse_numbers(prs)
    specs = fetch_pr_specs(
        numbers,
        workspace=workspace,
        repo_slug=repo_slug,
        username=settings.bitbucket_username,
        token=settings.bitbucket_token,
        unscored=set(_parse_numbers(unscored)),
        tune=set(_parse_numbers(tune)),
    )
    cases: list[BenchPRCase] = []
    for spec in specs:
        spec.db_name = f"{db_prefix}{spec.number}"
        db_settings = settings.model_copy(update={"db_name": spec.db_name})
        with connection(db_settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT repo_id, last_indexed_commit FROM repos ORDER BY repo_id")
                row = cur.fetchone()
            if row is None:
                typer.echo(f"PR #{spec.number}: {spec.db_name} has no indexed repo, skipping")
                continue
            repo_id, spec.indexed_commit = str(row[0]), str(row[1])
            repository = GraphRepository(GraphClient(conn))
            truth = build_ground_truth(repo, spec, repository, repo_id)
        cases.append(BenchPRCase(spec=spec, truth=truth, tasks=task_variants(spec)))
        typer.echo(
            f"PR #{spec.number} [{spec.split}]: snapshot {spec.indexed_commit[:8]} -> "
            f"{len(truth.symbols)} symbols / {len(truth.files)} files "
            f"({len(truth.skipped)} paths skipped){'' if spec.scored else ' [not scored]'}"
        )
    save_cases(cases, out)
    typer.echo(f"Wrote {len(cases)} cases to {out}")


@bench_prs_app.command("run")
def bench_prs_run(
    cases: Path = typer.Option(..., "--cases", help="cases.json written by `bench-prs prepare`"),
    out: Path = typer.Option(..., "--out", help="JSON report path"),
    markdown: Path | None = typer.Option(None, "--markdown", help="Also write a Markdown report"),
    k: str = typer.Option(
        "10",
        "--k",
        help=(
            "Results per system (Recall@K etc.). A comma list such as '5,10,20' runs once at the "
            "largest K and reports every K as an extra column"
        ),
    ),
    variants: str = typer.Option(
        "full,title", "--variants", help="Task text variants: full (title+description), title"
    ),
    from_report: Path | None = typer.Option(
        None,
        "--from-report",
        help="Re-score the returned lists of a previous JSON report instead of retrieving again",
    ),
    rerun_bce: bool = typer.Option(
        False,
        "--rerun-bce",
        help=(
            "With --from-report: keep the stored voyage lists and semantic pool (no embedding API "
            "call) but run BCE again - the loop for engine changes"
        ),
    ),
    only: str = typer.Option("", "--only", help="Run just these PR numbers (default: all cases)"),
    tune: str = typer.Option("", "--tune", help=f"{_TUNE_HELP}; overrides the split in cases.json"),
) -> None:
    """Replay the cases: voyage_raw / voyage / bce -> per-PR metrics + aggregate gain."""
    from bce.bench.pr_ablation import (
        load_cases,
        render_markdown,
        replay_from_report,
        run_ablation,
    )

    selected = tuple(v.strip() for v in variants.split(",") if v.strip())
    ks = _parse_numbers(k)
    if not ks:
        raise typer.BadParameter("--k needs at least one positive integer", param_hint="--k")
    replay = replay_from_report(from_report) if from_report is not None else None

    bench_cases = load_cases(cases)
    tune_numbers = set(_parse_numbers(tune))
    if tune_numbers:
        for case in bench_cases:
            case.spec.split = "tune" if case.spec.number in tune_numbers else "holdout"
    only_numbers = set(_parse_numbers(only))
    if only_numbers:
        bench_cases = [c for c in bench_cases if c.spec.number in only_numbers]

    def _progress(outcome: object) -> None:
        o = outcome  # CaseOutcome
        v = o.systems["voyage"]["metrics"]  # type: ignore[attr-defined]
        b = o.systems["bce"]  # type: ignore[attr-defined]
        typer.echo(
            f"PR #{o.number} [{o.variant}] voyage R={v['recall']:.2f} MRR={v['mrr']:.2f} | "  # type: ignore[attr-defined]
            f"bce R={b['metrics']['recall']:.2f} MRR={b['metrics']['mrr']:.2f} "
            f"({b['latency_ms']:.0f} ms)"
        )

    report = run_ablation(
        bench_cases,
        k=max(ks),
        ks=tuple(ks) if len(ks) > 1 else (),
        variants=selected,
        progress=_progress,
        replay=replay,
        rerun_bce=rerun_bce,
    )
    data = report.to_dict()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(f"Wrote JSON report to {out}")
    if markdown is not None:
        markdown.write_text(render_markdown(report), encoding="utf-8")
        typer.echo(f"Wrote Markdown report to {markdown}")
    for variant in selected:
        for label, agg in [
            ("all", data["summary"][variant]),
            ("tune", data["summary_by_split"]["tune"][variant]),
            ("holdout", data["summary_by_split"]["holdout"][variant]),
        ]:
            if not agg["cases"]:
                continue
            v, b = agg["systems"]["voyage"], agg["systems"]["bce"]
            typer.echo(
                f"[{variant}/{label}] {agg['cases']} PRs | recall {v['recall'] * 100:.1f}% -> "
                f"{b['recall'] * 100:.1f}% | MRR {v['mrr']:.2f} -> {b['mrr']:.2f} | "
                f"hit {v['hit'] * 100:.1f}% -> {b['hit'] * 100:.1f}% | "
                f"retention {b['semantic_retention'] * 100:.1f}% | "
                f"bce {b['latency_median_ms']:.0f} ms"
            )


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
