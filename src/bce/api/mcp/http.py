"""MCP over streamable HTTP: the stdio tool catalog, served by uvicorn.

stdio ties the server to the agent's machine (the agent spawns it). Over HTTP one long-running
engine serves agents in other processes or containers, e.g. a benchmark sidecar or a team server.

Security model:

- Bearer tokens (``BCE_MCP_HTTP_TOKEN``, comma-separated so a token can be rotated without
  downtime). Every request except ``GET /healthz`` must carry ``Authorization: Bearer <token>``;
  tokens are compared in constant time and a miss answers 401 without detail.
- No token, no network: without a token the server binds only to a loopback address and refuses
  any other host at startup, so an unauthenticated engine is never reachable off the machine.
- DNS rebinding: on loopback without a token the Host and Origin headers must name a loopback
  address, so a web page cannot reach the engine through a rebound domain. With a token the check
  applies when ``BCE_MCP_HTTP_ALLOWED_HOSTS`` / ``_ORIGINS`` are set; a browser cannot present a
  token it does not know.
- The tool catalog is the stdio one: read-only unless ``BCE_MCP_ALLOW_WRITE`` is on.

Next to the MCP endpoint the app answers ``POST /v1/precontext`` (same token): a Claude Code
``UserPromptSubmit`` hook's JSON in, the hook's response out
(:func:`bce.integrations.precontext.server_hook_response`), so a client in another process or
container gets the pre-computed context with ``curl`` alone.

TLS is left to a reverse proxy; bind to loopback (or a private network) behind it.
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

HEALTH_PATH = "/healthz"
PRECONTEXT_PATH = "/v1/precontext"
#: ``(hook JSON, repo_ids, mode) -> hook response`` (``{}``: nothing to add).
Precontext = Callable[[dict[str, Any], list[str] | None, str | None], dict[str, Any]]
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class HttpSecurityError(ValueError):
    """The requested HTTP bind is not safe with the configured tokens."""


def parse_list(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_bind(host: str, tokens: Iterable[str]) -> None:
    if not tuple(tokens) and not is_loopback(host):
        raise HttpSecurityError(
            f"refusing to serve MCP over HTTP on {host!r} without a token: set "
            "BCE_MCP_HTTP_TOKEN, or bind to 127.0.0.1"
        )


def security_settings(
    host: str,
    tokens: Iterable[str],
    allowed_hosts: Iterable[str] = (),
    allowed_origins: Iterable[str] = (),
) -> dict[str, Any]:
    """The SDK's DNS-rebinding settings for this bind, as keyword arguments."""
    hosts, origins = list(allowed_hosts), list(allowed_origins)
    if not hosts and not tuple(tokens) and is_loopback(host):
        hosts = [h for name in LOOPBACK_HOSTS for h in (name, f"{name}:*")]
        origins = origins or [f"http://{name}:*" for name in LOOPBACK_HOSTS]
    return {
        "enable_dns_rebinding_protection": bool(hosts),
        "allowed_hosts": hosts,
        "allowed_origins": origins,
    }


def _bearer(scope: dict[str, Any]) -> bytes | None:
    for key, value in scope.get("headers", ()):
        if key == b"authorization":
            scheme, _, credential = value.partition(b" ")
            if scheme.lower() == b"bearer" and credential.strip():
                return credential.strip()
            return None
    return None


class BearerAuthMiddleware:
    """ASGI middleware: every HTTP request outside ``exempt_paths`` must carry a known token."""

    def __init__(self, app: Any, tokens: Iterable[str], exempt_paths: Iterable[str] = ()) -> None:
        self.app = app
        self._tokens = tuple(token.encode() for token in tokens)
        self._exempt = frozenset(exempt_paths)

    def _accepts(self, presented: bytes | None) -> bool:
        if presented is None:
            return False
        matched = False
        for token in self._tokens:  # no early exit: timing must not reveal which token matched
            matched |= hmac.compare_digest(presented, token)
        return matched

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or not self._tokens
            or scope.get("path") in self._exempt
            or self._accepts(_bearer(scope))
        ):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "unauthorized"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="bce-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_http_app(
    server: Any,
    *,
    host: str,
    path: str = "/mcp",
    tokens: Iterable[str] = (),
    allowed_hosts: Iterable[str] = (),
    allowed_origins: Iterable[str] = (),
    precontext: Precontext | None = None,
) -> Any:
    """An ASGI app serving ``server`` at ``path`` (stateless streamable HTTP), a health probe and,
    with ``precontext``, the hook route."""
    import anyio.to_thread
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    tokens = tuple(tokens)
    check_bind(host, tokens)
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        security_settings=TransportSecuritySettings(
            **security_settings(host, tokens, allowed_hosts, allowed_origins)
        ),
    )

    class _Endpoint:  # a plain ASGI callable, so Starlette routes every method to it unwrapped
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await manager.handle_request(scope, receive, send)

    async def _health(_request: Any) -> Any:
        return JSONResponse({"status": "ok"})

    async def _precontext(request: Any) -> Any:
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict) or precontext is None:
            return JSONResponse({"error": "expected the hook's JSON object"}, status_code=400)
        repo_ids = list(request.query_params.getlist("repo")) or None
        mode = request.query_params.get("mode") or None
        t0 = time.perf_counter()
        try:
            out = await anyio.to_thread.run_sync(precontext, body, repo_ids, mode)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        context = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext")
        logging.getLogger("bce.api.mcp").info(
            "precontext hook",
            extra={
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "chars": len(context) if isinstance(context, str) else 0,
            },
        )
        return JSONResponse(out or {})

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any) -> Any:
        async with manager.run():
            yield

    routes = [Route(HEALTH_PATH, _health, methods=["GET"]), Route(path, _Endpoint())]
    if precontext is not None:
        routes.insert(1, Route(PRECONTEXT_PATH, _precontext, methods=["POST"]))
    app = Starlette(routes=routes, lifespan=_lifespan)
    return BearerAuthMiddleware(app, tokens, exempt_paths=(HEALTH_PATH,))
