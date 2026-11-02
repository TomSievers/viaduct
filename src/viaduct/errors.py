"""What a client reads when a tool fails.

The MCP library only passes on the text of a ToolError: any other exception reaches the client
as a bare "Error executing tool <name>". The tools and the modules below them raise plain
exceptions, so this one place turns those into readable messages, for every tool.
"""

from __future__ import annotations

import logging

from kipy.errors import ApiError, ConnectionError as KiCadConnectionError, FutureVersionError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

log = logging.getLogger(__name__)

# failures the code expects: bad input, a refusal, KiCad or the file system saying no
EXPECTED = (ValueError, RuntimeError, OSError, ApiError, KiCadConnectionError, FutureVersionError)


def expected(e: BaseException) -> bool:
    return isinstance(e, EXPECTED)


def describe(e: BaseException) -> str:
    """The message for a failed tool call. Anything unexpected is named as a bug, with its type."""
    if isinstance(e, KiCadConnectionError):
        return (f"{e}. Is KiCad running with a board open and its API on (Preferences > Plugins)? "
                "open_headless opens a board without KiCad's window")
    if isinstance(e, OSError) and e.strerror:      # without Python's "[Errno 2]"
        return f"{e.strerror}: {e.filename}" if e.filename else e.strerror
    if expected(e):
        return str(e) or type(e).__name__
    return f"internal error in Viaduct, probably a bug ({type(e).__name__}: {e})"


class ViaductServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as e:
            cause = e.__cause__ or e
            if not expected(cause):        # a ToolError is logged without its traceback
                log.error("Tool %r crashed", name, exc_info=cause)
            raise ToolError(f"Error executing tool {name}: {describe(cause)}") from cause
