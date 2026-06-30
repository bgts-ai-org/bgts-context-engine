"""Stable, deterministic identifier generation (spec section 4 / 5, step 3).

``symbol_id`` follows SCIP-moniker logic: ``language + package + namespace + name + signature``
hashed into a stable id that is **repo- and commit-independent**. This is what lets a CALLS edge
resolve to the same symbol across repos and across commits, and what makes tools chainable
(one tool's ``symbol_id`` output is another tool's input).

All other ids (file/repo/module/route/note) are derived deterministically as well, so the same
source at the same path always yields the same graph keys.
"""

from __future__ import annotations

import hashlib

_UNIT_SEP = "\x1f"
_DIGEST_BYTES = 8  # 16 hex chars / 64 bits - low collision risk for enterprise repos.


def _digest(*parts: str | None) -> str:
    payload = _UNIT_SEP.join("" if p is None else p for p in parts)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def make_symbol_id(
    *,
    language: str,
    package: str | None,
    namespace: str | None,
    name: str,
    signature: str | None = None,
    kind: str | None = None,
) -> str:
    """Build a stable symbol id.

    The readable moniker prefix aids debugging; the hash suffix guarantees uniqueness across
    overloads (different ``signature``) and disambiguates collisions in the readable part.
    """
    package_n = _norm(package)
    namespace_n = _norm(namespace)
    name_n = _norm(name)
    moniker = f"{language}::{package_n}::{namespace_n}::{name_n}"
    suffix = _digest(language, package_n, namespace_n, kind, name_n, signature)
    return f"{moniker}#{suffix}"


def make_repo_id(name: str) -> str:
    slug = _norm(name).lower().replace(" ", "-")
    return slug or _digest(name)


def make_file_id(repo_id: str, path: str) -> str:
    return f"{repo_id}:{_norm_path(path)}"


def make_module_id(repo_id: str, namespace: str) -> str:
    return f"{repo_id}:{_norm(namespace)}"


def make_route_id(repo_id: str, framework: str, http_method: str, path_pattern: str) -> str:
    return f"route:{repo_id}:{_digest(framework, http_method, path_pattern)}"


def make_note_id(file_id: str, line: int, kind: str, text: str) -> str:
    return f"note:{_digest(file_id, str(line), kind, text)}"
