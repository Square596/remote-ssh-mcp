"""Compatibility imports and feature detection for MCP SDK 1.x and 2.x."""

from __future__ import annotations

import inspect
import json
from functools import wraps
from typing import Any

try:
    from mcp.server import MCPServer
except ImportError as exc:
    if exc.name != "mcp.server":
        raise
    from mcp.server.fastmcp import FastMCP as MCPServer

try:
    from mcp.types import ToolAnnotations
except ImportError:
    ToolAnnotations = None


def tool(
    server: Any,
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
):
    """Register a typed tool using optional SDK features when available."""
    supported = inspect.signature(server.tool).parameters
    options: dict[str, Any] = {}
    has_structured_output = "structured_output" in supported
    if has_structured_output:
        options["structured_output"] = True
    if "annotations" in supported and ToolAnnotations is not None:
        options["annotations"] = ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=destructive,
            idempotentHint=idempotent,
            openWorldHint=open_world,
        )
    register = server.tool(**options)
    if not has_structured_output:
        return register

    def decorator(function):
        from mcp.types import CallToolResult, TextContent

        @wraps(function)
        async def sparse_structured_output(*args, **kwargs):
            result = await function(*args, **kwargs)
            content = json.dumps(result, ensure_ascii=False, indent=2)
            return CallToolResult(
                content=[TextContent(type="text", text=content)],
                structuredContent=result,
            )

        register(sparse_structured_output)
        return function

    return decorator


__all__ = ["MCPServer", "tool"]
