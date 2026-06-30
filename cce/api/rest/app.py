"""FastAPI application factory.

``create_app`` wires the router; ``app`` is a module-level instance so the server can be started
with ``uvicorn cce.api.rest.app:app`` (or via ``cce serve``). The app holds no state itself - every
request gets its own database connection through the ``get_repository`` dependency.
"""

from __future__ import annotations

from fastapi import FastAPI

from cce import __version__
from cce.api.rest.routes import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Cortex Context Engine",
        version=__version__,
        summary="Deterministic, multi-language code-graph context engine (Layer-1 REST surface).",
    )
    app.include_router(router)
    return app


app = create_app()
