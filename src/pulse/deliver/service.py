"""Deliver a validated week: append its section to the pulse Doc, then create a Gmail draft.

Order of work (T7.2–T7.5):

1. Load the week's validated pulse (`stats.json`, `pulse.json`, `pulse.txt`).
2. Plan every write from local state and re-scan every outgoing payload for personal data.
   Any hit blocks the whole delivery before the first MCP call (T7.4).
3. `--dry-run`: write the planned calls to `runs/<week>/delivery_payloads.json`, with the
   recipient and Doc ID masked, and stop. No MCP calls are made (T7.5).
4. Otherwise connect once, read the Doc (its URL and tail), append or resume the section,
   then create the draft if the week doesn't have one. Progress is saved after every write.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import partial
from pathlib import Path

import anyio

from pulse.compose.render import title_for
from pulse.config import DeliveryConfig, Settings
from pulse.deliver import docs, gmail
from pulse.deliver.mcp_client import DeliveryError, McpClient, ToolCall
from pulse.manifest import load_manifest, save_manifest
from pulse.models import PulseContent, PulseStats, RunManifest, RunRecord, utc_now
from pulse.privacy.scrubber import scan
from pulse.store import db
from pulse.timeutil import IST, parse_iso_week

STAGE = "deliver"
PAYLOADS_FILE = "delivery_payloads.json"
RECIPIENT_MASK = "<PULSE_RECIPIENT>"
DOC_ID_MASK = "<PULSE_DOC_ID>"

ClientFactory = Callable[[DeliveryConfig], McpClient]

_DOC_ID = re.compile(r"/document/d/([A-Za-z0-9_-]+)")


class PrivacyBlocked(DeliveryError):
    """An outgoing payload matches a personal-data pattern. Nothing was sent."""


@dataclass(frozen=True)
class Payload:
    """The week's validated pulse, as delivery needs it."""

    week: str
    section: docs.DocSection
    subject: str
    text: str


@dataclass
class DeliveryReport:
    week: str
    dry_run: bool
    doc: docs.DocOutcome | None = None
    draft: gmail.DraftOutcome | None = None
    calls: list[ToolCall] = field(default_factory=list)  # planned writes (and the Doc read)
    payload_file: Path | None = None


def doc_id_of(document: str) -> str:
    match = _DOC_ID.search(document)
    return match.group(1) if match else document.strip()


def doc_url_of(document: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id_of(document)}/edit"


# --- loading -------------------------------------------------------------------------------


def load_payload(settings: Settings, week: str, manifest: RunManifest | None) -> Payload:
    out = settings.paths.runs / week
    validate = manifest.stages.get("validate") if manifest else None
    if validate is None or validate.status != "done":
        raise DeliveryError(
            f"{week} has no validated pulse; run `pulse compose --week {week}` first",
            precondition=True,
        )
    try:
        stats = PulseStats.model_validate_json((out / "stats.json").read_text(encoding="utf-8"))
        content = PulseContent.model_validate_json((out / "pulse.json").read_text(encoding="utf-8"))
        text = (out / "pulse.txt").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DeliveryError(
            f"missing {exc.filename}; run `pulse compose --week {week}` again", precondition=True
        ) from exc
    section = docs.build_section(stats, content, min_words=settings.sources.filters.min_words)
    subject = title_for(settings.delivery.gmail.subject, parse_iso_week(week)[0])
    return Payload(week, section, subject, text)


# --- privacy re-scan (T7.4) ----------------------------------------------------------------


def outgoing_texts(calls: list[ToolCall], doc_url: str) -> list[tuple[str, str]]:
    """(where, text) for every piece of content that leaves the machine."""
    texts: list[tuple[str, str]] = []
    for call in calls:
        args = call.args
        if "text" in args:
            texts.append((f"{call.part}.text", args["text"]))
        for r, row in enumerate(args.get("rows", [])):
            texts.extend((f"{call.part}.rows[{r}][{c}]", cell) for c, cell in enumerate(row))
        if "subject" in args:
            texts.append((f"{call.part}.subject", args["subject"]))
        if "body" in args:
            # The Doc link is ours and is a URL by design; scan everything before it.
            body = args["body"].removesuffix(gmail.LINK_PREFIX + doc_url)
            texts.append((f"{call.part}.body", body))
    return texts


def privacy_check(calls: list[ToolCall], doc_url: str) -> None:
    hits = [
        f"{where} ({', '.join(sorted({hit.kind for hit in found}))})"
        for where, text in outgoing_texts(calls, doc_url)
        if (found := scan(text))
    ]
    if hits:
        raise PrivacyBlocked(
            "Delivery blocked: personal-data patterns in " + "; ".join(hits) + ". Nothing was sent."
        )


# --- dry run (T7.5) ------------------------------------------------------------------------


def mask(calls: list[ToolCall], *, document: str, recipient: str) -> list[dict[str, object]]:
    raw = json.dumps(
        [{"part": c.part, "op": c.op, "args": c.args} for c in calls], ensure_ascii=False
    )
    for secret, placeholder in (
        (document, DOC_ID_MASK),
        (doc_id_of(document), DOC_ID_MASK),
        (recipient, RECIPIENT_MASK),
    ):
        raw = raw.replace(json.dumps(secret)[1:-1], placeholder)
    masked: list[dict[str, object]] = json.loads(raw)
    return masked


def write_payloads(path: Path, calls: list[ToolCall], *, document: str, recipient: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = mask(calls, document=document, recipient=recipient)
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# --- delivery ------------------------------------------------------------------------------


def deliver_week(
    conn: sqlite3.Connection,
    settings: Settings,
    week: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    client_factory: ClientFactory | None = None,
    today: date | None = None,
) -> DeliveryReport:
    """Deliver (or dry-run) a week. Raises `DeliveryError` on failure; progress is kept."""
    settings.check_delivery_ready(dry_run=dry_run)
    return anyio.run(
        partial(
            _deliver,
            conn,
            settings,
            week,
            dry_run=dry_run,
            force=force,
            client_factory=client_factory or McpClient.from_settings,
            today=today or datetime.now(IST).date(),
        )
    )


async def _deliver(
    conn: sqlite3.Connection,
    settings: Settings,
    week: str,
    *,
    dry_run: bool,
    force: bool,
    client_factory: ClientFactory,
    today: date,
) -> DeliveryReport:
    runs = settings.paths.runs
    manifest = load_manifest(runs, week)
    payload = load_payload(settings, week, manifest)
    assert manifest is not None  # load_payload checked it
    document = settings.delivery.docs.document or ""
    recipient = settings.pulse.recipient or ""
    run = db.get_run(conn, week)
    state = manifest.delivery
    report = DeliveryReport(week, dry_run)

    def plan(doc_has_week: bool, doc_url: str) -> tuple[docs.SectionPlan, ToolCall | None]:
        section = docs.plan_section(
            payload.section,
            state,
            document=document,
            week=week,
            doc_has_week=doc_has_week,
            force=force,
            today=today,
        )
        draft = gmail.plan_draft(
            state,
            week=week,
            recipient=recipient,
            subject=payload.subject,
            pulse_text=payload.text,
            doc_url=doc_url,
            force=force,
        )
        return section, draft

    def planned(section: docs.SectionPlan, draft: ToolCall | None) -> list[ToolCall]:
        return [docs.info_call(document), *section.calls, *([draft] if draft else [])]

    # Plan from local state and scan before connecting: a hit means zero MCP calls.
    stored_end = run.doc_section_end_index if run else None
    doc_url = state.doc_url or doc_url_of(document)
    section_plan, draft = plan(stored_end is not None, doc_url)
    report.calls = planned(section_plan, draft)
    privacy_check(report.calls, doc_url)

    if dry_run:
        report.payload_file = write_payloads(
            runs / week / PAYLOADS_FILE, report.calls, document=document, recipient=recipient
        )
        return report

    def save() -> None:
        save_manifest(runs, manifest)

    stage = manifest.start(STAGE)
    save()
    try:
        async with client_factory(settings.delivery) as client:
            info = await docs.read_doc(client, document)
            doc_url = str(info["url"])
            has_week = stored_end is not None or docs.tail_has_week(info, payload.section.title)
            section_plan, draft = plan(has_week, doc_url)
            report.calls = planned(section_plan, draft)
            privacy_check(report.calls, doc_url)

            report.doc = await docs.publish_section(client, section_plan, state, info, save=save)
            if report.doc.status == "skipped":
                stage.flags.append("doc_already_has_week")
            run = _record_doc(conn, manifest, run, report.doc)

            report.draft = await gmail.create_draft(client, draft, state)
            if report.draft.status == "skipped":
                stage.flags.append("draft_already_exists")
            _record_draft(conn, run, report.draft)
    except DeliveryError as exc:
        manifest.fail(STAGE, str(exc))
        save()
        raise
    except Exception as exc:
        manifest.fail(STAGE, f"{type(exc).__name__}: {exc}")
        save()
        raise
    manifest.finish(
        STAGE,
        counts={
            "doc_parts_written": len(section_plan.calls),
            "drafts_created": int(report.draft.status == "created"),
        },
    )
    save()
    return report


def _record_doc(
    conn: sqlite3.Connection,
    manifest: RunManifest,
    run: RunRecord | None,
    outcome: docs.DocOutcome,
) -> RunRecord:
    if run is None:
        run = RunRecord(
            week=manifest.week,
            window_start=manifest.window_start,
            window_end=manifest.window_end,
            status="doc_delivered",
        )
    update: dict[str, object] = {
        "doc_id": outcome.doc_id,
        "doc_url": outcome.doc_url,
        "updated_at": utc_now(),
    }
    if outcome.status != "skipped":
        update.update(
            status="doc_delivered",
            doc_section_start_index=outcome.start_index,
            doc_section_end_index=outcome.end_index,
            doc_revision_id=outcome.revision_id,
        )
    run = run.model_copy(update=update)
    db.upsert_run(conn, run)
    return run


def _record_draft(conn: sqlite3.Connection, run: RunRecord, outcome: gmail.DraftOutcome) -> None:
    run = run.model_copy(
        update={
            "status": "delivered",
            "draft_id": outcome.draft_id,
            "draft_message_id": outcome.message_id,
            "updated_at": utc_now(),
        }
    )
    db.upsert_run(conn, run)
