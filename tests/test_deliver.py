"""Phase 7: MCP client, Doc section, Gmail draft, privacy re-scan and dry run (T7.1–T7.6).

Everything runs against the in-memory fake of `gmail-docs-mcp` (tests/fakes); one group of
tests goes through the Streamable HTTP transport with a bearer token.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import Client

from pulse.config import ConfigError, DeliveryConfig, PathsConfig, Settings, parse_settings
from pulse.deliver import docs
from pulse.deliver.gmail import LINK_PREFIX
from pulse.deliver.mcp_client import (
    DeliveryError,
    McpClient,
    SendToolRefused,
    ToolCall,
    ToolCallError,
)
from pulse.deliver.service import (
    DOC_ID_MASK,
    PAYLOADS_FILE,
    RECIPIENT_MASK,
    PrivacyBlocked,
    deliver_week,
    doc_id_of,
    mask,
)
from pulse.manifest import load_manifest, open_manifest, save_manifest
from pulse.models import (
    Action,
    PulseContent,
    PulseStats,
    QuotePick,
    Takeaway,
    ThemeStats,
)
from pulse.store import db
from pulse.timeutil import window_for
from tests.conftest import SETTINGS_PATH
from tests.fakes.fake_gmail_docs_server import (
    TOKEN,
    URL,
    Failure,
    FakeGmailDocsServer,
    load_tools,
)

WEEK = "2026-W41"
TITLE = "Groww Review Pulse — Week of 05 Oct 2026"
RECIPIENT = "pulse-team@example.com"
TODAY = date(2026, 10, 18)
DOC_PARTS = 8  # title, source, 3 headings, table, quotes, actions


# --- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeGmailDocsServer:
    return FakeGmailDocsServer()


@pytest.fixture
def settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeGmailDocsServer
) -> Settings:
    monkeypatch.setenv("PULSE_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("PULSE_DOC_ID", fake.doc_id)
    monkeypatch.setenv("PULSE_RECIPIENT", RECIPIENT)
    raw = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    loaded = parse_settings(raw)
    paths = PathsConfig(db=tmp_path / "pulse.db", runs=tmp_path / "runs")
    return loaded.model_copy(update={"paths": paths})


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = db.connect(settings.paths.db)
    yield connection
    connection.close()


def write_pulse(settings: Settings, *, takeaway: str = "Withdrawals stuck for days") -> None:
    """A validated week on disk, as `pulse compose` leaves it."""
    start, end = window_for(WEEK, 12)
    stats = PulseStats(
        week=WEEK,
        window_start=start,
        window_end=end,
        total_reviews=1534,
        avg_rating=2.9,
        classified_n=1534,
        other_share=0.05,
        themes=[
            ThemeStats(
                theme_id="pay",
                name="Withdrawals & Payments",
                count=500,
                share=0.34,
                avg_rating=1.4,
                pct_negative=0.9,
                latest_week_count=40,
                weekly_avg=30,
                trend="up",
            ),
            ThemeStats(
                theme_id="app",
                name="App Performance",
                count=300,
                share=0.2,
                avg_rating=1.8,
                pct_negative=0.8,
                latest_week_count=25,
                weekly_avg=25,
                trend="flat",
            ),
            ThemeStats(
                theme_id="ease",
                name="Ease of Use",
                count=250,
                share=0.16,
                avg_rating=4.8,
                pct_negative=0.0,
                latest_week_count=5,
                weekly_avg=20,
                trend=None,
            ),
        ],
        top_theme_ids=["pay", "app", "ease"],
    )
    content = PulseContent(
        week=WEEK,
        title=TITLE,
        takeaways=[
            Takeaway(theme_id="pay", text=takeaway),
            Takeaway(theme_id="app", text="App hangs at market open"),
            Takeaway(theme_id="ease", text="Beginners like the clean interface"),
        ],
        quotes=[
            QuotePick(
                theme_id="pay", review_id="r1", quote="Withdrawal pending for days", rating=1
            ),
            QuotePick(theme_id="app", review_id="r2", quote="App hangs every morning", rating=1),
            QuotePick(theme_id="ease", review_id="r3", quote="Easy to use for beginners", rating=5),
        ],
        actions=[
            Action(theme_id="pay", text="Show withdrawal status with an expected date"),
            Action(theme_id="app", text="Load-test the order screen before market open"),
            Action(theme_id="ease", text="Keep the simple home screen in redesigns"),
        ],
    )
    out = settings.paths.runs / WEEK
    out.mkdir(parents=True, exist_ok=True)
    (out / "stats.json").write_text(stats.model_dump_json(), encoding="utf-8")
    (out / "pulse.json").write_text(content.model_dump_json(), encoding="utf-8")
    (out / "pulse.txt").write_text(f"{TITLE}\nSource: ...\nTOP THEMES\n...\n", encoding="utf-8")
    manifest = open_manifest(settings.paths.runs, WEEK, start, end)
    manifest.start("validate")
    manifest.finish("validate", counts={"word_count": 180})
    save_manifest(settings.paths.runs, manifest)


def factory(fake: FakeGmailDocsServer, timeout: float = 5) -> Any:
    def make(delivery: DeliveryConfig) -> McpClient:
        return McpClient(fake.server, tools=delivery.tool_map(), timeout_seconds=timeout)

    return make


def deliver(
    conn: sqlite3.Connection,
    settings: Settings,
    fake: FakeGmailDocsServer,
    **kwargs: Any,
) -> Any:
    return deliver_week(conn, settings, WEEK, client_factory=factory(fake), today=TODAY, **kwargs)


TOOL_MAP = {
    "docs.info": "gdocs_get_document_info",
    "docs.append_text": "gdocs_append_text",
    "docs.append_table": "gdocs_append_table",
    "gmail.create_draft": "gmail_create_draft",
}


# --- T7.6 contract -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_lists_the_contract_tools(fake: FakeGmailDocsServer) -> None:
    expected = load_tools()
    async with Client(fake.server) as client:
        listing = await client.list_tools()
    assert [t.name for t in listing.tools] == [t["name"] for t in expected]
    for tool, spec in zip(listing.tools, expected, strict=True):
        assert tool.input_schema == spec["input_schema"]
        assert tool.output_schema == spec["output_schema"]
    assert {t["name"] for t in expected} == {
        "gmail_send_email",
        "gdocs_get_document_info",
        "gdocs_append_text",
        "gdocs_append_table",
        "gmail_create_draft",
    }


def test_planned_calls_fit_the_tool_schemas(settings: Settings, conn: sqlite3.Connection) -> None:
    """Every argument the pipeline sends exists in the server's input schema."""
    write_pulse(settings)
    report = deliver(conn, settings, FakeGmailDocsServer(), dry_run=True)
    schemas = {t["name"]: t["input_schema"] for t in load_tools()}
    for call in report.calls:
        schema = schemas[TOOL_MAP[call.op]]
        assert set(call.args) <= set(schema["properties"]), call.part
        assert set(schema.get("required", [])) <= set(call.args), call.part
        assert len(call.args.get("client_request_id", "")) <= 128


# --- T7.1 client ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_reads_structured_content(fake: FakeGmailDocsServer) -> None:
    async with McpClient(fake.server, tools=TOOL_MAP) as client:
        info = await client.call("docs.info", {"document": fake.doc_id})
    assert info["document_id"] == fake.doc_id
    assert info["url"] == fake.url


@pytest.mark.asyncio
async def test_missing_tool_fails_before_any_write() -> None:
    fake = FakeGmailDocsServer(drafts_enabled=False)
    with pytest.raises(DeliveryError) as err:
        async with McpClient(fake.server, tools=TOOL_MAP):
            pass
    assert err.value.precondition
    assert "gmail_create_draft" in str(err.value) and "ENABLE_DRAFTS=true" in str(err.value)
    assert fake.calls == []


def test_send_tools_cannot_be_mapped() -> None:
    with pytest.raises(SendToolRefused):
        McpClient("https://example.invalid/mcp", tools={"gmail.create_draft": "gmail_send_email"})
    raw = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    raw["delivery"]["gmail"]["tools"]["create_draft"] = "gmail_send_email"
    with pytest.raises(ConfigError, match="send tool"):
        parse_settings(raw)


@pytest.mark.asyncio
async def test_unmapped_operations_cannot_reach_send(fake: FakeGmailDocsServer) -> None:
    async with McpClient(fake.server, tools=TOOL_MAP) as client:
        with pytest.raises(DeliveryError, match="no tool mapped"):
            await client.call("gmail.send_email", {"to": ["a@example.com"]})
    assert fake.sent == [] and fake.calls == []


@pytest.mark.asyncio
async def test_is_error_maps_to_tool_call_error(fake: FakeGmailDocsServer) -> None:
    fake.fail_at(1, Failure("is_error", "Google Docs rejected the request: bad request."))
    async with McpClient(fake.server, tools=TOOL_MAP) as client:
        with pytest.raises(ToolCallError, match="bad request") as err:
            await client.call("docs.info", {"document": fake.doc_id})
    assert not err.value.retryable and not err.value.precondition


@pytest.mark.asyncio
async def test_blocked_recipient_is_a_precondition_with_the_fix() -> None:
    fake = FakeGmailDocsServer(allowed_domains=["groww.in"])
    async with McpClient(fake.server, tools=TOOL_MAP) as client:
        with pytest.raises(ToolCallError) as err:
            await client.call(
                "gmail.create_draft", {"to": [RECIPIENT], "subject": "s", "body": "b"}
            )
    assert err.value.precondition
    assert "EMAIL_ALLOWED_DOMAINS" in str(err.value)
    assert fake.drafts == []


@pytest.mark.asyncio
async def test_timeout_is_retried_once_then_retryable(fake: FakeGmailDocsServer) -> None:
    fake.fail_at(1, Failure("timeout", times=2))
    async with McpClient(fake.server, tools=TOOL_MAP, timeout_seconds=0.2) as client:
        with pytest.raises(DeliveryError) as err:
            await client.call("docs.info", {"document": fake.doc_id})
    assert err.value.retryable
    assert fake.tool_names() == ["gdocs_get_document_info"] * 2


# --- T7.1 over HTTP ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_transport_with_bearer_token(fake: FakeGmailDocsServer) -> None:
    transport, inner = fake.http_transport()
    async with inner.router.lifespan_context(inner):
        client = McpClient(URL, tools=TOOL_MAP, token=TOKEN, http_transport=transport)
        async with client:
            info = await client.call("docs.info", {"document": fake.doc_id})
    assert info["url"] == fake.url


@pytest.mark.asyncio
async def test_http_401_points_at_the_token(fake: FakeGmailDocsServer) -> None:
    transport, inner = fake.http_transport()
    async with inner.router.lifespan_context(inner):
        client = McpClient(URL, tools=TOOL_MAP, token="wrong", http_transport=transport)
        with pytest.raises(DeliveryError, match="PULSE_MCP_TOKEN") as err:
            async with client:
                pass
    assert err.value.precondition
    assert fake.calls == []


@pytest.mark.asyncio
async def test_http_5xx_write_is_retried_with_the_same_request_id(
    fake: FakeGmailDocsServer,
) -> None:
    fake.fail_at(1, Failure("http", status=503))
    transport, inner = fake.http_transport()
    args = {"document": fake.doc_id, "text": "hello", "client_request_id": "pulse:x:1"}
    async with inner.router.lifespan_context(inner):
        client = McpClient(URL, tools=TOOL_MAP, token=TOKEN, http_transport=transport)
        async with client:
            result = await client.call("docs.append_text", args)
    assert result["end_index"] > result["start_index"]
    assert [c.tool for c in fake.calls] == ["<http>", "gdocs_append_text"]
    assert len(fake.elements) == 1


# --- T7.2 / T7.3 delivery ------------------------------------------------------------------


def test_first_delivery_appends_the_section_then_drafts(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    write_pulse(settings)
    report = deliver(conn, settings, fake)

    assert fake.tool_names() == [
        "gdocs_get_document_info",
        "gdocs_append_text",
        "gdocs_append_text",
        "gdocs_append_text",
        "gdocs_append_table",
        "gdocs_append_text",
        "gdocs_append_text",
        "gdocs_append_text",
        "gdocs_append_text",
        "gmail_create_draft",
    ]
    styles = [(e.kind, e.style) for e in fake.elements]
    assert styles == [
        ("text", "HEADING_1"),
        ("text", "NORMAL_TEXT"),
        ("text", "HEADING_2"),
        ("table", None),
        ("text", "HEADING_2"),
        ("text", "NORMAL_TEXT"),
        ("text", "HEADING_2"),
        ("text", "NORMAL_TEXT"),
    ]
    assert fake.elements[0].text == TITLE
    assert fake.elements[1].text.startswith("Source: 1,534 English Play Store reviews")
    assert fake.elements[3].rows == [
        ["Theme", "Share", "Trend", "Takeaway"],
        ["Withdrawals & Payments", "34%", "▲", "Withdrawals stuck for days"],
        ["App Performance", "20%", "▬", "App hangs at market open"],
        ["Ease of Use", "16%", "", "Beginners like the clean interface"],
    ]
    assert [e.text for e in fake.elements[4:]] == [
        "What users said",
        '"Withdrawal pending for days" — 1★, Withdrawals & Payments\n'
        '"App hangs every morning" — 1★, App Performance\n'
        '"Easy to use for beginners" — 5★, Ease of Use',
        "Action ideas",
        "1. Show withdrawal status with an expected date\n"
        "2. Load-test the order screen before market open\n"
        "3. Keep the simple home screen in redesigns",
    ]

    draft = fake.drafts[0]
    assert draft["to"] == [RECIPIENT] and draft["body_format"] == "text"
    assert draft["subject"] == TITLE
    assert draft["body"].endswith(LINK_PREFIX + fake.url)
    assert fake.sent == [] and not any("send" in name for name in fake.tool_names())

    assert report.doc.status == "appended" and report.draft.status == "created"
    run = db.get_run(conn, WEEK)
    assert run is not None and run.status == "delivered"
    assert run.doc_id == fake.doc_id and run.doc_url == fake.url
    assert run.doc_section_start_index is not None
    assert run.doc_section_end_index == fake.end_index
    assert run.draft_id == "r-draft-1" and run.draft_message_id == "msg-1"
    manifest = load_manifest(settings.paths.runs, WEEK)
    assert manifest is not None and manifest.stages["deliver"].status == "done"
    section = manifest.delivery.section
    assert section is not None and section.complete and len(section.parts) == DOC_PARTS
    # Every write carried a deterministic client_request_id for this week
    ids = [c.args["client_request_id"] for c in fake.writes()]
    assert all(i.startswith(f"pulse:{WEEK}:") for i in ids) and len(set(ids)) == len(ids)


def test_rerun_is_a_no_op(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    write_pulse(settings)
    deliver(conn, settings, fake)
    before = len(fake.calls)
    report = deliver(conn, settings, fake)
    assert fake.tool_names()[before:] == ["gdocs_get_document_info"]
    assert len(fake.drafts) == 1
    assert report.doc.status == "skipped" and report.draft.status == "skipped"
    manifest = load_manifest(settings.paths.runs, WEEK)
    assert manifest is not None
    assert {"doc_already_has_week", "draft_already_exists"} <= set(manifest.stages["deliver"].flags)


def test_resume_after_failure_at_part_4(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    write_pulse(settings)
    fake.fail_at(5, Failure("is_error"))  # call 1 reads the Doc; call 5 is part 4, the table
    with pytest.raises(DeliveryError):
        deliver(conn, settings, fake)
    assert len(fake.elements) == 3 and fake.drafts == []
    manifest = load_manifest(settings.paths.runs, WEEK)
    assert manifest is not None and manifest.stages["deliver"].status == "failed"
    assert set(manifest.delivery.sections[0].parts) == {"title", "source", "themes_heading"}

    before = len(fake.calls)
    report = deliver(conn, settings, fake)
    resent = fake.calls[before:]
    assert [c.tool for c in resent] == [
        "gdocs_get_document_info",
        "gdocs_append_table",
        *["gdocs_append_text"] * 4,
        "gmail_create_draft",
    ]
    assert report.doc.status == "resumed"
    assert [e.text for e in fake.elements].count(TITLE) == 1
    assert len(fake.elements) == DOC_PARTS and len(fake.drafts) == 1


def test_skips_when_the_doc_already_has_the_week(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    """No local state (say the runs folder was lost), but the Doc's tail has the heading."""
    write_pulse(settings)
    fake.initial_text += f"{TITLE}\nSource: earlier delivery\n"
    report = deliver(conn, settings, fake)
    assert report.doc.status == "skipped"
    assert fake.elements == []
    assert report.draft.status == "created"


def test_force_appends_an_updated_section_and_a_new_draft(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    write_pulse(settings)
    deliver(conn, settings, fake)
    report = deliver(conn, settings, fake, force=True)
    assert report.doc.status == "appended" and report.draft.status == "created"
    titles = [e.text for e in fake.elements if e.style == "HEADING_1"]
    assert titles == [TITLE, f"{TITLE} (updated 18 Oct 2026)"]
    assert len(fake.elements) == 2 * DOC_PARTS
    assert len(fake.drafts) == 2
    assert not any(c.deduplicated for c in fake.calls)
    run = db.get_run(conn, WEEK)
    assert run is not None and run.draft_id == "r-draft-2"


def test_blocked_recipient_keeps_the_doc_section(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    write_pulse(settings)
    fake = FakeGmailDocsServer(allowed_domains=["groww.in"])
    with pytest.raises(DeliveryError, match="EMAIL_ALLOWED_DOMAINS") as err:
        deliver(conn, settings, fake)
    assert err.value.precondition
    assert len(fake.elements) == DOC_PARTS and fake.drafts == []
    run = db.get_run(conn, WEEK)
    assert run is not None and run.status == "doc_delivered" and run.draft_id is None


def test_needs_a_validated_pulse(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    with pytest.raises(DeliveryError, match="pulse compose") as err:
        deliver(conn, settings, fake)
    assert err.value.precondition and fake.calls == []


def test_delivery_settings_are_required(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    write_pulse(settings)
    unset = settings.model_copy(
        update={"pulse": settings.pulse.model_copy(update={"recipient": None})}
    )
    with pytest.raises(ConfigError, match="PULSE_RECIPIENT"):
        deliver(conn, unset, fake)
    assert fake.calls == []


# --- T7.4 privacy re-scan ------------------------------------------------------------------


def test_phone_number_in_a_table_cell_blocks_delivery(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    write_pulse(settings, takeaway="Users asked to call 9876543210 for refunds")
    with pytest.raises(PrivacyBlocked, match=r"doc:themes_table\.rows\[1\]\[3\] \(PHONE\)") as err:
        deliver(conn, settings, fake)
    assert "9876543210" not in str(err.value)
    assert fake.calls == []


def test_privacy_scan_covers_the_dry_run_too(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    write_pulse(settings, takeaway="Mail me at someone@example.com")
    with pytest.raises(PrivacyBlocked):
        deliver(conn, settings, fake, dry_run=True)
    assert not (settings.paths.runs / WEEK / PAYLOADS_FILE).exists()


# --- T7.5 dry run --------------------------------------------------------------------------


def test_dry_run_matches_a_real_delivery_call_for_call(
    settings: Settings, conn: sqlite3.Connection, fake: FakeGmailDocsServer
) -> None:
    write_pulse(settings)
    report = deliver(conn, settings, fake, dry_run=True)
    assert fake.calls == []
    assert report.payload_file == settings.paths.runs / WEEK / PAYLOADS_FILE
    planned = json.loads(report.payload_file.read_text(encoding="utf-8"))
    text = report.payload_file.read_text(encoding="utf-8")
    assert fake.doc_id not in text and RECIPIENT not in text
    assert DOC_ID_MASK in text and RECIPIENT_MASK in text

    deliver(conn, settings, fake)
    tools = {v: k for k, v in TOOL_MAP.items()}
    received = mask(
        [
            ToolCall(part=p["part"], op=tools[c.tool], args=c.args)
            for p, c in zip(planned, fake.calls, strict=True)
        ],
        document=fake.doc_id,
        recipient=RECIPIENT,
    )
    assert received == planned


# --- helpers -------------------------------------------------------------------------------


def test_doc_id_of_accepts_urls_and_ids() -> None:
    assert doc_id_of("https://docs.google.com/document/d/1AbC_x-9/edit?tab=t.0") == "1AbC_x-9"
    assert doc_id_of(" 1AbC_x-9 ") == "1AbC_x-9"


def test_plan_resumes_only_a_matching_section() -> None:
    from pulse.models import DeliveryState, DocSectionState

    base = docs.DocSection(TITLE, "Source: x", [["Theme"]], ["q"], ["1. a"])
    state = DeliveryState(sections=[DocSectionState(title=TITLE, sha="stale", parts={})])
    with pytest.raises(docs.SectionMismatch):
        docs.plan_section(
            base, state, document="d", week=WEEK, doc_has_week=True, force=False, today=TODAY
        )
    plan = docs.plan_section(
        base, state, document="d", week=WEEK, doc_has_week=True, force=True, today=TODAY
    )
    assert plan.state is not None and plan.state.title.endswith("(updated 18 Oct 2026)")
    assert len(plan.calls) == DOC_PARTS


def test_section_hash_changes_with_sequence() -> None:
    base = docs.DocSection(TITLE, "Source: x", [["Theme"]], ["q"], ["1. a"])
    assert base.sha(0) != base.sha(1)
