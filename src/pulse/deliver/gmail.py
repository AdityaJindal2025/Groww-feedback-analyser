"""Gmail draft creator (T7.3).

Creates a draft only, never sends: the client refuses any tool with "send" in its name. The
draft is created after the Doc section, so its link always points at content that exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pulse.deliver.mcp_client import McpClient, ToolCall
from pulse.models import DeliveryState

LINK_PREFIX = "\n\nFull pulse: "

DraftStatus = Literal["created", "skipped"]


def draft_body(pulse_text: str, doc_url: str) -> str:
    return pulse_text.rstrip() + LINK_PREFIX + doc_url


def draft_call(
    *,
    week: str,
    recipient: str,
    subject: str,
    body: str,
    previous_draft_id: str | None = None,
) -> ToolCall:
    """The `gmail_create_draft` call. A forced re-run hashes in the previous draft's ID, so the
    server doesn't de-duplicate it into the earlier draft."""
    args = {"to": [recipient], "subject": subject, "body": body, "body_format": "text"}
    digest = hashlib.sha256(
        json.dumps({**args, "previous": previous_draft_id}, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return ToolCall(
        part="draft",
        op="gmail.create_draft",
        args={**args, "client_request_id": f"pulse:{week}:draft:{digest[:12]}"},
    )


def plan_draft(
    delivery: DeliveryState,
    *,
    week: str,
    recipient: str,
    subject: str,
    pulse_text: str,
    doc_url: str,
    force: bool,
) -> ToolCall | None:
    """None when the week already has a draft (unless `force`)."""
    if delivery.draft_id and not force:
        return None
    return draft_call(
        week=week,
        recipient=recipient,
        subject=subject,
        body=draft_body(pulse_text, doc_url),
        previous_draft_id=delivery.draft_id,
    )


@dataclass(frozen=True)
class DraftOutcome:
    status: DraftStatus
    draft_id: str | None
    message_id: str | None


async def create_draft(
    client: McpClient, call: ToolCall | None, delivery: DeliveryState
) -> DraftOutcome:
    if call is None:
        return DraftOutcome("skipped", delivery.draft_id, delivery.message_id)
    result = await client.call(call.op, call.args)
    delivery.draft_id = str(result["draft_id"])
    delivery.message_id = str(result["message_id"])
    return DraftOutcome("created", delivery.draft_id, delivery.message_id)
