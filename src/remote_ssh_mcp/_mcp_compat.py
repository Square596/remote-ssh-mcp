"""Compatibility imports for MCP SDK 1.x and 2.x."""

try:
    from mcp.server import MCPServer
except ImportError as exc:
    if exc.name != "mcp.server":
        raise
    from mcp.server.fastmcp import FastMCP as MCPServer

__all__ = ["MCPServer"]
