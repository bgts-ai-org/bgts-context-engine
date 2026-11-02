"""MCP over streamable HTTP: bearer auth, the loopback-only rule and DNS-rebinding checks."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from bce.api.mcp.http import (  # noqa: E402
    HttpSecurityError,
    build_http_app,
    check_bind,
    is_loopback,
    security_settings,
)

LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def _server():
    from mcp.server import Server
    from mcp.types import Tool

    server = Server("bce-test")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [Tool(name="ping", description="ping", inputSchema={"type": "object"})]

    return server


def _rpc_result(response) -> dict:
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("application/json"):
        return response.json()["result"]
    data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1])["result"]


def _client(host: str = "127.0.0.1", **kwargs) -> TestClient:
    app = build_http_app(_server(), host=host, **kwargs)
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"])
def test_loopback_hosts(host):
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::", "bce", "example.com"])
def test_non_loopback_hosts(host):
    assert not is_loopback(host)


def test_no_token_means_loopback_only():
    check_bind("127.0.0.1", ())
    check_bind("0.0.0.0", ("secret",))
    with pytest.raises(HttpSecurityError, match="BCE_MCP_HTTP_TOKEN"):
        check_bind("0.0.0.0", ())
    with pytest.raises(HttpSecurityError):
        build_http_app(_server(), host="0.0.0.0")


def test_health_needs_no_token():
    with _client(tokens=("secret",)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "authorization", [None, "Bearer wrong", "Basic c2VjcmV0", "Bearer ", "secret"]
)
def test_a_request_without_a_known_token_is_401(authorization):
    headers = dict(MCP_HEADERS)
    if authorization is not None:
        headers["authorization"] = authorization
    with _client(tokens=("secret",)) as client:
        response = client.post("/mcp", json=LIST_TOOLS, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json() == {"error": "unauthorized"}


def test_every_configured_token_is_accepted():
    with _client(host="0.0.0.0", tokens=("old", "new")) as client:
        for token in ("old", "new"):
            response = client.post(
                "/mcp", json=LIST_TOOLS, headers={**MCP_HEADERS, "authorization": f"bearer {token}"}
            )
            assert [t["name"] for t in _rpc_result(response)["tools"]] == ["ping"]


def test_loopback_without_token_rejects_a_rebound_host():
    with _client() as client:
        ok = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS)
        rebound = client.post(
            "/mcp", json=LIST_TOOLS, headers={**MCP_HEADERS, "host": "evil.example:8765"}
        )
        foreign_origin = client.post(
            "/mcp", json=LIST_TOOLS, headers={**MCP_HEADERS, "origin": "http://evil.example"}
        )
    assert [t["name"] for t in _rpc_result(ok)["tools"]] == ["ping"]
    assert rebound.status_code == 421
    assert foreign_origin.status_code == 403


def test_the_precontext_route_answers_a_hook_behind_the_token():
    seen = []

    def precontext(body, repo_ids, mode):
        seen.append((body, repo_ids, mode))
        return {
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "x"}
        }

    hook = {"prompt": "Fix the login timeout in the auth module", "cwd": "/w"}
    with _client(host="0.0.0.0", tokens=("t",), precontext=precontext) as client:
        denied = client.post("/v1/precontext", json=hook)
        ok = client.post(
            "/v1/precontext?repo=r1&repo=r2", json=hook, headers={"authorization": "Bearer t"}
        )
        bad = client.post("/v1/precontext", content=b"[1]", headers={"authorization": "Bearer t"})
    assert denied.status_code == 401
    assert ok.json()["hookSpecificOutput"]["additionalContext"] == "x"
    assert seen == [(hook, ["r1", "r2"], None)]
    assert bad.status_code == 400


def test_without_a_precontext_handler_there_is_no_route():
    with _client() as client:
        assert client.post("/v1/precontext", json={}).status_code in (404, 405)


def test_security_settings_follow_the_configuration():
    assert security_settings("127.0.0.1", ())["enable_dns_rebinding_protection"]
    assert not security_settings("0.0.0.0", ("t",))["enable_dns_rebinding_protection"]
    explicit = security_settings("0.0.0.0", ("t",), allowed_hosts=("bce:*",))
    assert explicit["enable_dns_rebinding_protection"]
    assert explicit["allowed_hosts"] == ["bce:*"]


def test_cli_refuses_an_open_bind_without_a_token(monkeypatch):
    from typer.testing import CliRunner

    from bce.cli import app
    from bce.config import get_settings

    monkeypatch.setenv("BCE_MCP_HTTP_TOKEN", "")
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["serve-mcp", "--transport", "http", "--host", "0.0.0.0"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 2
    assert "BCE_MCP_HTTP_TOKEN" in result.output


def test_cli_rejects_an_unknown_transport():
    from typer.testing import CliRunner

    from bce.cli import app

    result = CliRunner().invoke(app, ["serve-mcp", "--transport", "websocket"])
    assert result.exit_code == 2
