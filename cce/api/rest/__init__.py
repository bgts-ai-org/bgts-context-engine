"""REST adapter (FastAPI).

A thin transport over the Layer-1 tools. It does not implement retrieval/graph logic itself; it
opens a per-request database connection, resolves the request locale (i18n presentation layer), and
delegates to ``cce.tools``. Import :func:`create_app` (factory) or ``app`` (module-level instance,
for ``uvicorn cce.api.rest.app:app``).
"""

from cce.api.rest.app import app, create_app

__all__ = ["app", "create_app"]
