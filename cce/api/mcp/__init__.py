"""MCP server adapter (spec section 7: agent-native surface).

MCP is a *thin* adapter over the same core the REST API uses - no logic is duplicated. The tool
catalog (:mod:`cce.api.mcp.tools`) is SDK-independent so it can be introspected and tested without
the optional ``mcp`` package installed; :func:`cce.api.mcp.server.build_server` wires it into a real
MCP server when the SDK is present.
"""

from cce.api.mcp.tools import TOOL_SPECS, dispatch_tool

__all__ = ["TOOL_SPECS", "dispatch_tool"]
