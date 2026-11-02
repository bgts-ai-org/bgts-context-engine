"""``bce doctor``: the installation's health and each indexed repository's index quality report.

:func:`collect` gathers the facts (database reachability, pending migrations, installed parsers,
embedding and retrieval configuration, and the ``repo_index_stats`` report of every repository);
:func:`render` turns them into the text the command prints. Both are pure apart from the reads
:func:`collect` makes, so the text is testable without a database.
"""

from __future__ import annotations

import importlib
from typing import Any

from bce.indexing.index_stats import REPORT_RATIO, UNSUPPORTED_RATIO


def parsers() -> dict[str, list[str]]:
    """Installed languages, and the optional ones whose grammar is not installed."""
    from bce.indexing.parser.registry import _OPTIONAL_PROVIDERS, build_default_registry

    installed = list(build_default_registry().languages())
    missing = []
    for module_path, _ in _OPTIONAL_PROVIDERS:
        language = module_path.rsplit(".", 1)[-1].removesuffix("_provider")
        try:
            importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 - a missing grammar is the thing being reported
            missing.append(language)
    return {"installed": installed, "missing": missing}


def collect(conn: Any | None, *, repo_ids: list[str] | None = None, error: str | None = None):
    """The doctor report; ``conn`` None (with ``error``) when the database is unreachable."""
    from bce.config import get_settings
    from bce.core.orchestrator.profile import active_profile
    from bce.indexing.index_stats import read_index_stats, stats_available
    from bce.storage.relational.migrator import MIGRATIONS_DIR

    settings = get_settings()
    profile = active_profile(settings)
    report: dict[str, Any] = {
        "database": {
            "ok": conn is not None,
            "target": f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}",
            "error": error,
        },
        "parsers": parsers(),
        "embedding": {
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "dim": settings.embedding_dim,
        },
        "retrieval": {"profile": profile.name, "expansion": profile.expansion},
        "repos": [],
    }
    if conn is None:
        return report
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        tracked = bool((cur.fetchone() or [False])[0])
        applied: set[str] = set()
        if tracked:
            cur.execute("SELECT id FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
        all_ids = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
        report["migrations"] = {
            "applied": len([m for m in all_ids if m in applied]),
            "pending": [m for m in all_ids if m not in applied],
        }
        cur.execute("SELECT to_regclass('public.repos') IS NOT NULL")
        names: dict[str, tuple[str, str | None]] = {}
        if bool((cur.fetchone() or [False])[0]):
            cur.execute("SELECT repo_id, name, last_indexed_commit FROM repos ORDER BY repo_id")
            names = {str(r[0]): (str(r[1]), r[2]) for r in cur.fetchall()}
    stats = read_index_stats(conn, repo_ids) if stats_available(conn) else {}
    for repo_id in sorted(set(names) | set(stats)):
        if repo_ids and repo_id not in repo_ids:
            continue
        name, commit = names.get(repo_id, (repo_id, None))
        row = stats.get(repo_id) or {}
        report["repos"].append(
            {
                "repo_id": repo_id,
                "name": name,
                "commit": row.get("commit") or commit,
                "computed_at": str(row["computed_at"]) if row.get("computed_at") else None,
                "stats": row.get("stats"),
            }
        )
    return report


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f} %" if whole else "-"


def _short(commit: Any) -> str:
    text = str(commit or "?")
    return text[:7] if len(text) >= 12 else text


def render(report: dict[str, Any]) -> list[str]:
    """The text ``bce doctor`` prints, one list item per line."""
    db = report["database"]
    out = ["BCE doctor"]
    out.append(
        f"  database     {'ok' if db['ok'] else 'unreachable'} ({db['target']})"
        + (f": {db['error']}" if db.get("error") else "")
    )
    migrations = report.get("migrations")
    if migrations is not None:
        pending = migrations["pending"]
        out.append(
            f"  migrations   {migrations['applied']} applied"
            + (
                f", pending: {', '.join(pending)} (run `bce migrate`)"
                if pending
                else ", none pending"
            )
        )
    parsed = report["parsers"]
    line = "  parsers      " + ", ".join(parsed["installed"])
    if parsed["missing"]:
        line += f" (not installed: {', '.join(parsed['missing'])}; pip install -e '.[langs]')"
    out.append(line)
    emb = report["embedding"]
    out.append(f"  embeddings   {emb['provider']} · {emb['model']} (dim {emb['dim']})")
    ret = report["retrieval"]
    out.append(f"  retrieval    profile {ret['profile']} · expansion {ret['expansion']}")
    for repo in report.get("repos") or []:
        out.append("")
        out.extend(_repo_lines(repo))
    if db["ok"] and not report.get("repos"):
        out.append("")
        out.append("No repository is indexed yet (`bce index --repo PATH --name NAME`).")
    return out


def _repo_lines(repo: dict[str, Any]) -> list[str]:
    head = f"repo {repo['name']} @ {_short(repo.get('commit'))}"
    if repo.get("computed_at"):
        head += f" (report {str(repo['computed_at'])[:16]})"
    stats = repo.get("stats")
    if not stats:
        return [head, "  no index report: re-index the repository to create one"]
    files, indexed = int(stats.get("files") or 0), int(stats.get("indexed") or 0)
    ratio = indexed / files if files else 1.0
    out = [head, f"  code files   {files}, parsed {indexed} ({_pct(indexed, files)})"]
    dirs = stats.get("unparsed_dirs") or {}
    for lang, row in sorted(
        (stats.get("languages") or {}).items(), key=lambda kv: (-kv[1].get("files", 0), kv[0])
    ):
        lang_files, lang_indexed = int(row.get("files") or 0), int(row.get("indexed") or 0)
        text = f"    {lang:<12} {lang_indexed:>6} / {lang_files:<6}"
        if row.get("parse_errors"):
            text += f" parse errors {row['parse_errors']}"
        if lang_indexed < lang_files and dirs.get(lang):
            text += " not parsed: " + ", ".join(dirs[lang])
        out.append(text.rstrip())
    out.append(f"  symbols      {int(stats.get('symbols') or 0)}")
    refs = stats.get("references") or {}
    cross, resolved = int(refs.get("cross_file") or 0), int(refs.get("resolved") or 0)
    out.append(
        f"  cross-file references {cross}, resolved {resolved} ({_pct(resolved, cross)}; "
        "the rest point outside the repository or could not be bound)"
    )
    out.append(f"  scip         {'yes' if stats.get('scip') else 'no'}")
    emb = stats.get("embedding") or {}
    if emb:
        state = f"{emb.get('provider')} · {emb.get('model')}" if emb.get("enabled") else "off"
        out.append(f"  embeddings   {state}")
    if ratio < UNSUPPORTED_RATIO:
        out.append(
            "  verdict      unsupported: answers report confidence `unsupported`; agents should "
            "rely on their own search in this repository"
        )
    elif ratio < REPORT_RATIO:
        out.append("  verdict      partial: answers say which languages the index does not parse")
    else:
        out.append("  verdict      ok")
    return out
