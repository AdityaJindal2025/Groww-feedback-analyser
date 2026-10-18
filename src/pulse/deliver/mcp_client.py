"""MCP client for our `gmail-docs-mcp` server (T7.1).

One connection per delivery, over Streamable HTTP with a bearer token. `list_tools()` runs
first, so a missing tool fails before anything is written. Code calls logical operations
(`docs.append_text`), which are mapped to server tool names from settings.

Send guard: any tool whose name contains "send" can't be mapped or called. The server does
expose `gmail_send_email`; the pipeline only ever creates drafts.

Errors never carry Doc or email text: the server's error messages don't include it, and this
module adds only tool names and HTTP status codes.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import anyio
import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.shared._httpx_utils import create_mcp_http_client

from pulse.config import DeliveryConfig

# Fix hints for tools the server registers only when configured to.
_MISSING_HINTS = {
    "gmail_create_draft": "set ENABLE_DRAFTS=true on the server and re-run its `auth` "
    "(tick gmail.compose), then update GOOGLE_TOKEN_JSON on Railway",
}
# Server error messages that mean "fix the server's configuration", with the fix.
_PRECONDITION_HINTS = {
    "Blocked recipient": "add the recipient's domain to EMAIL_ALLOWED_DOMAINS on the server",
    "not in DOCS_ALLOWED_IDS": "add the pulse Doc's ID to DOCS_ALLOWED_IDS on the server",
    "Not signed in to Google": "run `auth` for the server and update GOOGLE_TOKEN_JSON",
    "sign-in has expired": "run `auth` for the server and update GOOGLE_TOKEN_JSON",
    "does not have edit access": "share the pulse Doc as Editor with the server's Google account",
}


class DeliveryError(RuntimeError):
    """Delivery failed. `retryable`: try again later; `precondition`: fix configuration first."""

    def __init__(self, message: str, *, retryable: bool = False, precondition: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.precondition = precondition


class SendToolRefused(DeliveryError):
    def __init__(self, tool: str):
        super().__init__(f"refusing to call {tool!r}: the pipeline never sends email, drafts only")


class ToolCallError(DeliveryError):
    """The server returned `isError`. The message is the server's, plus a fix hint if known."""


def refuse_send(tool: str) -> None:
    if "send" in tool.lower():
        raise SendToolRefused(tool)


@dataclass(frozen=True)
class ToolCall:
    """One planned write, in the order it is made."""

    part: str  # e.g. "doc:title", "draft"
    op: str  # logical operation, e.g. "docs.append_text"
    args: dict[str, Any]


class McpClient:
    """Async context manager: connect, list tools, check the mapping, then `call()`.

    `server` is the endpoint URL in production. Tests pass an in-process `Server`, or a URL
    plus an `http_transport` (an ASGI transport) to exercise the HTTP path.
    """

    def __init__(
        self,
        server: str | Server[Any],
        *,
        tools: Mapping[str, str],
        token: str | None = None,
        timeout_seconds: float = 60,
        http_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        for tool in tools.values():
            refuse_send(tool)
        self._server = server
        self._tools = dict(tools)
        self._token = token
        self._timeout = timeout_seconds
        self._http_transport = http_transport
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self._last_status: int | None = None

    @classmethod
    def from_settings(cls, delivery: DeliveryConfig) -> McpClient:
        if not delivery.mcp.token:
            raise DeliveryError(
                "PULSE_MCP_TOKEN is not set: put the server's MCP_HTTP_BEARER_TOKEN in .env",
                precondition=True,
            )
        return cls(
            delivery.mcp.url,
            tools=delivery.tool_map(),
            token=delivery.mcp.token,
            timeout_seconds=delivery.mcp.timeout_seconds,
        )

    # --- connection ------------------------------------------------------------------------

    async def __aenter__(self) -> McpClient:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            client = await stack.enter_async_context(self._make_client())
            with anyio.fail_after(self._timeout):
                listing = await client.list_tools()
        except Exception as exc:
            await stack.__aexit__(None, None, None)
            if isinstance(exc, DeliveryError):
                raise
            raise self._error("connecting to the MCP server", exc) from exc
        self._stack, self._client = stack, client
        try:
            self._check_tools({tool.name for tool in listing.tools})
        except DeliveryError:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            # Closing a broken connection must not mask the error that broke it.
            with contextlib.suppress(Exception):
                await stack.__aexit__(None, None, None)

    def _make_client(self) -> Client:
        if not isinstance(self._server, str):
            return Client(self._server)
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None

        async def record_status(response: httpx2.Response) -> None:
            self._last_status = response.status_code

        if self._http_transport is not None:
            http = httpx2.AsyncClient(headers=headers, transport=self._http_transport)
        else:
            http = create_mcp_http_client(headers=headers)
        http.event_hooks["response"].append(record_status)
        return Client(streamable_http_client(self._server, http_client=http))

    def _check_tools(self, listed: set[str]) -> None:
        missing = [f"{op} -> {tool}" for op, tool in self._tools.items() if tool not in listed]
        if missing:
            hints = [
                f"{tool} missing: {hint}"
                for tool, hint in _MISSING_HINTS.items()
                if tool in self._tools.values() and tool not in listed
            ]
            raise DeliveryError(
                "The MCP server doesn't list these tools: "
                + ", ".join(missing)
                + (". " + "; ".join(hints) if hints else ". Check delivery.*.tools in settings"),
                precondition=True,
            )

    # --- calls -----------------------------------------------------------------------------

    def tool_for(self, op: str) -> str:
        if op not in self._tools:
            raise DeliveryError(f"no tool mapped for {op!r}")
        tool = self._tools[op]
        refuse_send(tool)
        return tool

    async def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call a mapped tool and return its `structuredContent`.

        A connection failure, timeout or HTTP 5xx is retried once, immediately, with the same
        arguments, so a write keeps its `client_request_id` and the server de-duplicates it.
        An `isError` result is never retried here.
        """
        tool = self.tool_for(op)
        try:
            return await self._call_once(tool, args)
        except ToolCallError:
            raise
        except DeliveryError as exc:
            if not exc.retryable:
                raise
        return await self._call_once(tool, args)

    async def _call_once(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise DeliveryError("MCP client used outside `async with`")
        self._last_status = None
        try:
            with anyio.fail_after(self._timeout):
                result = await self._client.call_tool(tool, args)
        except Exception as exc:
            raise self._error(f"calling {tool}", exc) from exc
        if result.is_error:
            raise _tool_error(tool, result)
        if result.structured_content is None:
            raise DeliveryError(f"{tool} returned no structuredContent")
        return dict(result.structured_content)

    def _error(self, doing: str, exc: BaseException) -> DeliveryError:
        leaves = list(_leaves(exc))
        if self._last_status == 401 or any(
            isinstance(e, MCPError) and "unauthorized" in str(e).lower() for e in leaves
        ):
            return DeliveryError(
                f"The MCP server rejected the bearer token (HTTP 401) while {doing}: "
                "check PULSE_MCP_TOKEN",
                precondition=True,
            )
        status = self._last_status
        if (status is not None and status >= 500) or any(
            isinstance(e, TimeoutError | httpx2.TransportError) for e in leaves
        ):
            what = f"HTTP {status}" if status and status >= 500 else _describe(leaves)
            return DeliveryError(
                f"The MCP server is unreachable or failing ({what}) while {doing}; "
                "retry with `pulse deliver`",
                retryable=True,
            )
        return DeliveryError(f"MCP error while {doing}: {_describe(leaves)}")


def _tool_error(tool: str, result: Any) -> ToolCallError:
    message = " ".join(
        getattr(block, "text", "") for block in result.content if getattr(block, "text", "")
    ).strip()
    message = message or "no message"
    for marker, hint in _PRECONDITION_HINTS.items():
        if marker.lower() in message.lower():
            return ToolCallError(f"{tool}: {message} Fix: {hint}.", precondition=True)
    unavailable = "did not respond successfully" in message or "rate-limiting" in message
    return ToolCallError(f"{tool}: {message}", retryable=unavailable)


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _describe(leaves: list[BaseException]) -> str:
    return "; ".join(f"{type(e).__name__}: {e}" for e in leaves) or "unknown error"
