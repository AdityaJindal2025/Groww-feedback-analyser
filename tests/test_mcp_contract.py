"""T9.5 MCP contract check against `tests/fixtures/mcp/tools.json`.

Offline: the fake server lists exactly the fixture's tools (also in test_deliver.py).
Live (`pytest -m live`): the deployed Railway server lists the same tools with the same
schemas. A renamed tool or a new required field in the server repo fails here before it fails a
Monday run. Needs `PULSE_MCP_TOKEN` in `.env`; reads only.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from pulse import env
from pulse.config import load_settings
from tests.conftest import REPO_ROOT, SETTINGS_PATH
from tests.fakes.fake_gmail_docs_server import FakeGmailDocsServer, load_tools


def as_contract(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "input_schema": t.input_schema,
            "output_schema": t.output_schema,
        }
        for t in tools
    ]


def expected() -> list[dict[str, Any]]:
    return [
        {"name": t["name"], "input_schema": t["input_schema"], "output_schema": t["output_schema"]}
        for t in load_tools()
    ]


@pytest.mark.asyncio
async def test_fake_server_matches_the_contract() -> None:
    async with Client(FakeGmailDocsServer().server) as client:
        listing = await client.list_tools()
    assert as_contract(listing.tools) == expected()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_server_matches_the_contract() -> None:
    env.load_env(REPO_ROOT / ".env")
    token = env.optional(env.PULSE_MCP_TOKEN)
    if token is None:
        pytest.skip("PULSE_MCP_TOKEN is not set")
    url = load_settings(SETTINGS_PATH).delivery.mcp.url
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with Client(streamable_http_client(url, http_client=http)) as client:
        listing = await client.list_tools()
    live = sorted(as_contract(listing.tools), key=lambda t: t["name"])
    assert live == sorted(expected(), key=lambda t: t["name"]), (
        "The server's tools changed. Check the server repo's changelog, update the client if "
        "needed, then refresh the fixture with `python spikes/mcp_probe.py list`."
    )
