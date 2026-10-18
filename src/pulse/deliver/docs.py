"""Google Docs publisher: one running Doc, a new section per week (T7.2).

The server's Docs tools are append-only, so a week's section is written once, in parts, and
never edited. Idempotency comes from three things:

- per-part progress in the run manifest, so a failed delivery resumes at the first unfinished
  part without re-sending earlier ones;
- a check of the Doc's tail for this week's heading (and the section end stored in `runs`);
- a deterministic `client_request_id` per part, which the server de-duplicates for 24 hours.

`--force` appends a new section headed "… (updated <date>)"; the old one stays.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal

from pulse.analysis.aggregate import TREND_MARKERS
from pulse.compose.render import source_line
from pulse.deliver.mcp_client import DeliveryError, McpClient, ToolCall
from pulse.models import DeliveryState, DocSectionState, PartResult, PulseContent, PulseStats

TABLE_HEADER = ["Theme", "Share", "Trend", "Takeaway"]
TAIL_CHARS = 5000

# A section's parts, in the order they are appended.
PARTS: tuple[str, ...] = (
    "title",
    "source",
    "themes_heading",
    "themes_table",
    "quotes_heading",
    "quotes",
    "actions_heading",
    "actions",
)

DocStatus = Literal["appended", "resumed", "skipped"]


class SectionMismatch(DeliveryError):
    def __init__(self, message: str):
        super().__init__(message, precondition=True)


@dataclass(frozen=True)
class DocSection:
    """A week's section, with the same content as `pulse.md` in Docs layout."""

    title: str
    source: str
    table: list[list[str]]
    quotes: list[str]
    actions: list[str]

    def retitled(self, title: str) -> DocSection:
        return DocSection(title, self.source, self.table, self.quotes, self.actions)

    def sha(self, sequence: int = 0) -> str:
        """Content hash. `sequence` (the section's position for the week) keeps a forced
        re-delivery's `client_request_id`s apart from the original's."""
        payload = json.dumps({**asdict(self), "sequence": sequence}, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_section(stats: PulseStats, content: PulseContent, *, min_words: int) -> DocSection:
    themes = {theme.theme_id: theme for theme in stats.themes}
    takeaways = {t.theme_id: t.text for t in content.takeaways}
    table = [list(TABLE_HEADER)]
    for theme_id in stats.top_theme_ids:
        theme = themes[theme_id]
        table.append(
            [
                theme.name,
                f"{round(theme.share * 100)}%",
                TREND_MARKERS[theme.trend],
                takeaways.get(theme_id, ""),
            ]
        )
    names = {theme_id: theme.name for theme_id, theme in themes.items()}
    quotes = [
        f'"{q.quote}" — {q.rating}★, {names.get(q.theme_id, q.theme_id)}' for q in content.quotes
    ]
    actions = [f"{i}. {action.text}" for i, action in enumerate(content.actions, start=1)]
    return DocSection(
        title=content.title,
        source=f"Source: {source_line(stats, min_words)}",
        table=table,
        quotes=quotes,
        actions=actions,
    )


def updated_title(title: str, today: date) -> str:
    return f"{title} (updated {today:%d %b %Y})"


def request_id(week: str, sha: str, part: str) -> str:
    return f"pulse:{week}:{sha[:12]}:{part}"


def section_calls(section: DocSection, sha: str, *, document: str, week: str) -> list[ToolCall]:
    """Every write for a section, in order."""

    def text(part: str, value: str, style: str) -> ToolCall:
        return ToolCall(
            part=f"doc:{part}",
            op="docs.append_text",
            args={
                "document": document,
                "text": value,
                "paragraph_style": style,
                "start_on_new_line": True,
                "client_request_id": request_id(week, sha, part),
            },
        )

    return [
        text("title", section.title, "HEADING_1"),
        text("source", section.source, "NORMAL_TEXT"),
        text("themes_heading", "Top themes", "HEADING_2"),
        ToolCall(
            part="doc:themes_table",
            op="docs.append_table",
            args={
                "document": document,
                "rows": section.table,
                "client_request_id": request_id(week, sha, "themes_table"),
            },
        ),
        text("quotes_heading", "What users said", "HEADING_2"),
        text("quotes", "\n".join(section.quotes), "NORMAL_TEXT"),
        text("actions_heading", "Action ideas", "HEADING_2"),
        text("actions", "\n".join(section.actions), "NORMAL_TEXT"),
    ]


def info_call(document: str) -> ToolCall:
    return ToolCall(
        part="doc:info", op="docs.info", args={"document": document, "tail_chars": TAIL_CHARS}
    )


@dataclass(frozen=True)
class SectionPlan:
    """What a delivery will do with the Doc: nothing (skip), or write `calls` for `state`."""

    state: DocSectionState | None
    calls: list[ToolCall]
    resumed: bool = False

    @property
    def skip(self) -> bool:
        return self.state is None


def plan_section(
    base: DocSection,
    delivery: DeliveryState,
    *,
    document: str,
    week: str,
    doc_has_week: bool,
    force: bool,
    today: date,
) -> SectionPlan:
    """Decide whether to skip, resume or append (and under which title).

    `doc_has_week`: the Doc's tail (or the `runs` row) already shows this week's section.
    A dry run can only use the stored state.
    """
    current = delivery.section
    if current is not None and not current.complete:
        if current.title == base.title or current.title.startswith(f"{base.title} (updated "):
            section = base.retitled(current.title)
            sha = section.sha(len(delivery.sections) - 1)
            if sha == current.sha:
                calls = [
                    call
                    for call in section_calls(section, sha, document=document, week=week)
                    if call.part.removeprefix("doc:") not in current.parts
                ]
                return SectionPlan(current, calls, resumed=True)
        if not force:
            raise SectionMismatch(
                f"A partly delivered section for {week} ({len(current.parts)} of {len(PARTS)} "
                "parts) doesn't match the current pulse. Finish or remove it in the Doc by "
                "hand, then re-run with --force to append a fresh section."
            )
    delivered = (current is not None and current.complete) or doc_has_week
    if delivered and not force:
        return SectionPlan(None, [])
    section = base.retitled(updated_title(base.title, today)) if delivered else base
    sequence = len(delivery.sections)
    sha = section.sha(sequence)
    state = DocSectionState(title=section.title, sha=sha)
    return SectionPlan(state, section_calls(section, sha, document=document, week=week))


@dataclass(frozen=True)
class DocOutcome:
    status: DocStatus
    doc_id: str
    doc_url: str
    start_index: int | None
    end_index: int | None
    revision_id: str | None


async def read_doc(client: McpClient, document: str) -> dict[str, Any]:
    call = info_call(document)
    return await client.call(call.op, call.args)


def tail_has_week(info: dict[str, Any], title: str) -> bool:
    return title in str(info.get("tail_text", ""))


async def publish_section(
    client: McpClient,
    plan: SectionPlan,
    delivery: DeliveryState,
    info: dict[str, Any],
    *,
    save: Callable[[], None],
) -> DocOutcome:
    """Write the planned parts in order, saving progress after each one."""
    delivery.doc_id = str(info["document_id"])
    delivery.doc_url = str(info["url"])
    if plan.state is None:
        return DocOutcome("skipped", delivery.doc_id, delivery.doc_url, None, None, None)
    if not plan.resumed:
        delivery.sections.append(plan.state)
    state = plan.state
    save()
    for call in plan.calls:
        result = await client.call(call.op, call.args)
        state.parts[call.part.removeprefix("doc:")] = PartResult(
            start_index=int(result["start_index"]),
            end_index=int(result["end_index"]),
            revision_id=str(result["revision_id"]),
        )
        save()
    state.complete = all(part in state.parts for part in PARTS)
    save()
    first, last = state.parts[PARTS[0]], state.parts[PARTS[-1]]
    return DocOutcome(
        "resumed" if plan.resumed else "appended",
        delivery.doc_id,
        delivery.doc_url,
        first.start_index,
        last.end_index,
        last.revision_id,
    )
