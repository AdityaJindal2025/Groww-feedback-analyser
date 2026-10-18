"""In-memory fake of our `gmail-docs-mcp` server (T7.6).

Mirrors the real contract: the five tools with the input and output schemas in
`tests/fixtures/mcp/tools.json` (generated from the server's `tools/list`), arguments
validated against those schemas, `structuredContent` shaped like the server's, and
`client_request_id` de-duplication. It includes `gmail_send_email`, so tests can prove the
send guard holds.

It keeps an in-memory Doc and draft list, records every tool call, and can inject a failure
(`isError`, a timeout, or an HTTP status over the HTTP transport) at a chosen call.

Built on the SDK's low-level `Server` rather than `MCPServer`, so the listed schemas are exactly
the fixture's instead of being regenerated from Python signatures.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx2
import jsonschema
import mcp_types as types
from mcp.server.lowlevel import Server
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOOLS_JSON = Path(__file__).resolve().parent.parent / "fixtures" / "mcp" / "tools.json"
DOC_ID = "1FakePulseDoc_abcdefghijklmnopqrstuvwxyz0123"
TOKEN = "test-bearer-token"
URL = "http://127.0.0.1:8000/mcp"

FailureKind = Literal["is_error", "timeout", "http"]


def load_tools() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))
    return tools


@dataclass
class Failure:
    kind: FailureKind
    message: str = "Google Docs did not respond successfully. Retry in a moment."
    status: int = 500
    times: int = 1  # how many consecutive calls fail, starting at the chosen one


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    deduplicated: bool = False


@dataclass
class Element:
    kind: Literal["text", "table"]
    style: str | None
    text: str  # paragraphs joined by "\n"; a table renders as tab-separated rows
    rows: list[list[str]] | None = None


@dataclass
class FakeGmailDocsServer:
    drafts_enabled: bool = True
    doc_id: str = DOC_ID
    title: str = "Groww Review Pulse"
    allowed_domains: list[str] | None = None
    calls: list[Call] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    failures: dict[int, Failure] = field(default_factory=dict)
    initial_text: str = "Groww Review Pulse\n"

    def __post_init__(self) -> None:
        self.tools = [
            t for t in load_tools() if self.drafts_enabled or t["name"] != "gmail_create_draft"
        ]
        self._by_name = {t["name"]: t for t in self.tools}
        self._dedupe: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._revision = 0
        self._http_failure: Failure | None = None
        self.server: Server[Any] = Server(
            "fake-gmail-docs", on_list_tools=self._list_tools, on_call_tool=self._call_tool
        )

    # --- test helpers ----------------------------------------------------------------------

    def fail_at(self, n: int, failure: Failure) -> None:
        """Fail the n-th tool call (1-based, counting every tools/call)."""
        self.failures[n] = failure

    @property
    def url(self) -> str:
        return f"https://docs.google.com/document/d/{self.doc_id}/edit"

    @property
    def text(self) -> str:
        return self.initial_text + "".join(e.text + "\n" for e in self.elements)

    @property
    def end_index(self) -> int:
        return len(self.text) + 1

    def tool_names(self) -> list[str]:
        return [c.tool for c in self.calls]

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.tool != "gdocs_get_document_info"]

    # --- MCP handlers ----------------------------------------------------------------------

    async def _list_tools(
        self, ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=t["name"],
                    description=t["description"],
                    input_schema=t["input_schema"],
                    output_schema=t["output_schema"],
                    annotations=types.ToolAnnotations(**t["annotations"]),
                )
                for t in self.tools
            ]
        )

    async def _call_tool(
        self, ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        args = dict(params.arguments or {})
        call = Call(params.name, args)
        self.calls.append(call)
        failure = self._failure_for(len(self.calls))
        if failure is not None:
            if failure.kind == "timeout":
                await anyio.sleep(30)
            if failure.kind == "is_error":
                return _error(failure.message)
        spec = self._by_name.get(params.name)
        if spec is None:
            return _error(f"Unknown tool: {params.name}")
        try:
            jsonschema.validate(args, spec["input_schema"])
        except jsonschema.ValidationError as exc:
            return _error(f"Invalid input: {exc.message}")
        unknown = set(args) - set(spec["input_schema"]["properties"])
        if unknown:
            return _error(f"Invalid input: unknown fields {sorted(unknown)}")

        request_id = args.get("client_request_id")
        if request_id:
            digest = _hash({k: v for k, v in args.items() if k != "client_request_id"})
            cached = self._dedupe.get((params.name, request_id))
            if cached is not None:
                if cached[0] != digest:
                    return _error(
                        f'client_request_id "{request_id}" was already used with different '
                        "arguments. Use a new client_request_id for a different request."
                    )
                call.deduplicated = True
                return _ok(cached[1])

        try:
            result = self._run(params.name, args)
        except _ToolError as exc:
            return _error(str(exc))
        jsonschema.validate(result, spec["output_schema"])
        if request_id:
            self._dedupe[(params.name, request_id)] = (digest, result)
        return _ok(result)

    def _failure_for(self, n: int) -> Failure | None:
        for start, failure in self.failures.items():
            if start <= n < start + failure.times:
                return failure
        return None

    # --- tool behaviour --------------------------------------------------------------------

    def _run(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "gmail_send_email":
            self.sent.append(args)
            return {
                "status": "sent",
                "message_id": f"sent-{len(self.sent)}",
                "thread_id": "t",
                "to": args["to"],
                "cc": [],
                "bcc_count": 0,
                "subject": args["subject"],
            }
        if tool == "gmail_create_draft":
            self._check_recipients(args["to"])
            self.drafts.append(args)
            n = len(self.drafts)
            return {"draft_id": f"r-draft-{n}", "message_id": f"msg-{n}"}
        self._check_document(args["document"])
        if tool == "gdocs_get_document_info":
            tail = min(int(args.get("tail_chars", 500)), 5000)
            return {
                **self._doc_fields(),
                "tabs": [{"tab_id": "t.0", "title": "Tab 1"}],
                "end_index": self.end_index,
                "tail_text": self.text[-tail:],
            }
        start = self.end_index - 1
        if tool == "gdocs_append_text":
            self.elements.append(
                Element("text", args.get("paragraph_style", "NORMAL_TEXT"), args["text"])
            )
            return {
                **self._write_fields(start),
                "tab_id": "t.0",
                "inserted_chars": len(args["text"]),
            }
        if tool == "gdocs_append_table":
            rows = args["rows"]
            cols = max(len(r) for r in rows)
            padded = [r + [""] * (cols - len(r)) for r in rows]
            self.elements.append(
                Element("table", None, "\n".join("\t".join(r) for r in padded), padded)
            )
            return {
                **self._write_fields(start),
                "tab_id": "t.0",
                "row_count": len(rows),
                "column_count": cols,
            }
        raise _ToolError(f"Unknown tool: {tool}")

    def _check_document(self, document: str) -> None:
        if document not in (self.doc_id, self.url):
            raise _ToolError(f"Document {document} was not found.")

    def _check_recipients(self, to: list[str]) -> None:
        if self.allowed_domains is None:
            return
        blocked = [a for a in to if a.rsplit("@", 1)[-1] not in self.allowed_domains]
        if blocked:
            raise _ToolError(
                f"Blocked recipient(s): {', '.join(blocked)}. This server only sends to: "
                f"{', '.join(self.allowed_domains)}. Nothing was sent."
            )

    def _doc_fields(self) -> dict[str, Any]:
        return {
            "document_id": self.doc_id,
            "title": self.title,
            "url": self.url,
            "revision_id": f"rev-{self._revision}",
        }

    def _write_fields(self, start: int) -> dict[str, Any]:
        self._revision += 1
        return {**self._doc_fields(), "start_index": start, "end_index": self.end_index}

    # --- HTTP transport --------------------------------------------------------------------

    def asgi_app(self, token: str = TOKEN) -> tuple[ASGIApp, Any]:
        """(bearer-checking ASGI app, the inner Starlette app for its lifespan)."""
        inner = self.server.streamable_http_app(stateless_http=True, json_response=True)

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                headers = dict(scope["headers"])
                if headers.get(b"authorization") != f"Bearer {token}".encode():
                    await _plain(send, 401, b'{"error":"Unauthorized"}')
                    return
                body, receive = await _peek(receive)
                if b'"tools/call"' in body:
                    failure = self._failure_for(len(self.calls) + 1)
                    if failure is not None and failure.kind == "http":
                        self.calls.append(Call("<http>", {}))
                        await _plain(send, failure.status, b'{"error":"failed"}')
                        return
            await inner(scope, receive, send)

        return app, inner

    def http_transport(self, token: str = TOKEN) -> tuple[httpx2.ASGITransport, Any]:
        app, inner = self.asgi_app(token)
        return httpx2.ASGITransport(app=app), inner


class _ToolError(Exception):
    pass


def _ok(result: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")], structured_content=result
    )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def _plain(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _peek(receive: Receive) -> tuple[bytes, Receive]:
    """Read the request body, and return a `receive` that replays it."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = b"".join(chunks)
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay
