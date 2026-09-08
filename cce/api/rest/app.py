"""FastAPI application factory.

``create_app`` wires the router, the request-logging middleware, and the lifespan that runs the
background job workers; ``app`` is a module-level instance so the server can be started with
``uvicorn cce.api.rest.app:app`` (or via ``cce serve``). Every request gets its own database
connection through the ``get_repository`` dependency.

Request logging has two levels:

- INFO: one summary line per request (method, path, status, duration).
- DEBUG (``CCE_LOG_LEVEL=DEBUG``): additionally logs the request start, query params, the JSON
  request body (secrets masked, size-capped) and the response body (size-capped) for **every**
  endpoint.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response

from cce import __version__
from cce.api.rest.routes import router
from cce.config import get_settings
from cce.core.logging import get_logger, setup_logging

logger = get_logger("api.rest")

#: Request/response bodies logged at DEBUG are truncated to this many characters.
_BODY_LOG_LIMIT = 4000

#: Body keys whose values are masked in DEBUG logs (credentials must never reach the logs).
_SENSITIVE_KEYS = ("token", "password", "secret", "api_key", "username", "authorization")


def _mask(value: Any) -> Any:
    """Recursively mask sensitive fields in a decoded JSON structure."""
    if isinstance(value, dict):
        return {
            key: ("***" if any(s in key.lower() for s in _SENSITIVE_KEYS) and val else _mask(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_mask(item) for item in value]
    return value


def _body_for_log(raw: bytes) -> str:
    """Masked, truncated representation of a request/response body for DEBUG logs."""
    if not raw:
        return ""
    try:
        text = json.dumps(_mask(json.loads(raw)), ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        text = raw[:_BODY_LOG_LIMIT].decode("utf-8", errors="replace")
    if len(text) > _BODY_LOG_LIMIT:
        text = text[:_BODY_LOG_LIMIT] + f"... (+{len(text) - _BODY_LOG_LIMIT} chars)"
    return text


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the job worker pool with the server; drain it on shutdown.

    Shutdown is graceful: workers stop claiming new jobs and the in-flight job is awaited.
    """
    from cce.jobs.worker import JobWorkerPool

    setup_logging()
    pool = JobWorkerPool()
    pool.start()
    try:
        yield
    finally:
        pool.stop()


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="Cortex Context Engine",
        version=__version__,
        summary="Deterministic, multi-language code-graph context engine (Layer 1-2-3 REST surface).",
        lifespan=_lifespan,
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next) -> Response:
        debug = logger.isEnabledFor(logging.DEBUG)
        start = time.perf_counter()

        if debug:
            extra: dict[str, Any] = {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.query_params),
                "client": request.client.host if request.client else None,
            }
            # Safe with BaseHTTPMiddleware: Starlette caches the body and replays it downstream.
            raw = await request.body()
            if raw:
                extra["body"] = _body_for_log(raw)
            logger.debug("request started", extra=extra)

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )

        if debug and hasattr(response, "body_iterator"):
            chunks = [chunk async for chunk in response.body_iterator]
            raw_body = b"".join(chunks)
            logger.debug(
                "response body",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "size_bytes": len(raw_body),
                    "body": _body_for_log(raw_body),
                },
            )
            response = Response(
                content=raw_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
                background=response.background,
            )
        return response

    app.include_router(router)

    # --- UI layer (optional, self-contained; see cce/api/rest/ui/__init__.py) ---
    # To remove the UI layer: delete the cce/api/rest/ui package and this block.
    from fastapi.middleware.cors import CORSMiddleware

    from cce.api.rest.ui import ui_router

    app.include_router(ui_router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(get_settings().cors_origins),
        allow_methods=["GET", "POST"],  # POST: /v1/ui/context-trace (read-only pipeline trace).
        allow_headers=["*"],
    )
    # --- end UI layer ---

    return app


app = create_app()
