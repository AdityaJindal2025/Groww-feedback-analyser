"""T7.0: live probe of our deployed `gmail-docs-mcp` server. Not part of the package.

Reads the endpoint from config/settings.yaml (`delivery.mcp.url`) and the bearer token, Doc
and recipient from .env (`PULSE_MCP_TOKEN`, `PULSE_DOC_ID`, `PULSE_RECIPIENT`).

    python spikes/mcp_probe.py list    # tools -> tests/fixtures/mcp/tools.json (schemas only)
    python spikes/mcp_probe.py info    # gdocs_get_document_info on the pulse Doc
    python spikes/mcp_probe.py draft   # one "Pulse probe" draft; delete it by hand afterwards

Prints `structuredContent` and error results, never the token. Refuses any tool with "send" in
its name.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from pulse import env
from pulse.config import load_settings

TOOLS_JSON = Path("tests/fixtures/mcp/tools.json")


def connect() -> tuple[Client, Any]:
    settings = load_settings()
    token = env.require(env.PULSE_MCP_TOKEN)
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    return Client(streamable_http_client(settings.delivery.mcp.url, http_client=http)), settings


async def list_tools() -> None:
    client, _ = connect()
    async with client:
        result = await client.list_tools()
    tools = [
        {
            "name": t.name,
            "description": (t.description or "").strip(),
            "annotations": t.annotations.model_dump(by_alias=True, exclude_none=True)
            if t.annotations
            else {},
            "input_schema": t.input_schema,
            "output_schema": t.output_schema,
        }
        for t in result.tools
    ]
    TOOLS_JSON.parent.mkdir(parents=True, exist_ok=True)
    TOOLS_JSON.write_text(json.dumps(tools, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(tools)} tools -> {TOOLS_JSON}")
    for tool in tools:
        params = tool["input_schema"].get("properties", {})
        required = set(tool["input_schema"].get("required", []))
        args = ", ".join(f"{p}{'' if p in required else '?'}" for p in params)
        print(f"- {tool['name']}({args})")
    names = {t["name"] for t in tools}
    print("drafts enabled:", "gmail_create_draft" in names)


async def call(tool: str, arguments: dict[str, Any]) -> None:
    if "send" in tool.lower():
        raise SystemExit("refusing to call a send tool; drafts only")
    client, _ = connect()
    async with client:
        result = await client.call_tool(tool, arguments)
    print(f"is_error={result.is_error}")
    for block in result.content:
        print(getattr(block, "text", block))
    if result.structured_content is not None:
        print(json.dumps(result.structured_content, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["list", "info", "draft"])
    args = parser.parse_args()
    env.load_env()
    if args.cmd == "list":
        asyncio.run(list_tools())
    elif args.cmd == "info":
        document = env.require(env.PULSE_DOC_ID)
        asyncio.run(call("gdocs_get_document_info", {"document": document, "tail_chars": 500}))
    else:
        recipient = env.require(env.PULSE_RECIPIENT)
        asyncio.run(
            call(
                "gmail_create_draft",
                {
                    "to": [recipient],
                    "subject": "Pulse probe",
                    "body": "Probe draft from spikes/mcp_probe.py. Delete me.",
                    "body_format": "text",
                },
            )
        )


if __name__ == "__main__":
    main()
