"""T9.2 privacy end-to-end: personal data in the input never reaches any output.

The golden reviews plus reviews carrying fake emails, phone numbers, PAN- and Aadhaar-shaped
strings, a UPI handle, a ticket number, and a review naming a support agent. After a dry run and
a full run, nothing the scrubber patterns match may remain in the database, any file under
`runs/`, anything the fake MCP server received, or the logs. The review naming a person must
never be quoted.

Allow-listed, and nothing else: the pipeline's own hashed review IDs, the section hashes and
`client_request_id` digests it derives from them, the configured pulse Doc's ID and URL (masked
in the dry-run file), the configured recipient (the draft's `to`), and the test's temporary
directory (printed in file paths).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pulse.cli import app
from pulse.privacy.scrubber import scan
from pulse.store import db
from tests.golden_inputs import golden_rows, source
from tests.harness import RECIPIENT, offline

WEEK = "2026-W41"
AGENT_REVIEW = (
    "Withdrawal still pending and the agent named Priya on chat just kept telling me to wait"
)
PII_REVIEWS = [
    (1, "Withdrawal failed again, please email me at ravi.sharma88@example.com with the update"),
    (1, "Call me on +91 98765 43210 about my stuck refund, nobody has responded to me yet"),
    (1, "My PAN ABCDE1234F was rejected during KYC verification for no reason at all"),
    (1, "Aadhaar 2345 6789 0123 is linked but the KYC verification still shows pending today"),
    (1, "Sent money from myhandle@okaxis via UPI and the add money request is still stuck"),
    (1, "Ticket id 28036628 still open after ten days, customer care never responded to me"),
]
RAW_SECRETS = [
    "ravi.sharma88@example.com",
    "98765 43210",
    "ABCDE1234F",
    "2345 6789 0123",
    "myhandle@okaxis",
    "28036628",
]
# Names can't be caught by patterns: the review stays in the store, flagged `mentions_person`,
# and must not appear in any output.
AGENT_NAME = "Priya"


def write_inputs(path: Path) -> None:
    rows = golden_rows()
    extra = [
        {
            "id": f"pii-{i}",
            "rating": rating,
            "text": text,
            "date": "2026-10-10T12:00:00+05:30",
            "thumbs_up": 50,
        }
        for i, (rating, text) in enumerate(PII_REVIEWS)
    ]
    # Newest and most liked in its theme: it would be the first quote candidate if allowed.
    extra.append(
        {
            "id": "pii-agent",
            "rating": 1,
            "text": AGENT_REVIEW,
            "date": "2026-10-11T18:00:00+05:30",
            "thumbs_up": 99,
        }
    )
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows + extra) + "\n", encoding="utf-8"
    )


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    return []


def test_no_personal_data_in_any_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = tmp_path / "reviews.jsonl"
    write_inputs(inputs)
    env = offline(tmp_path, monkeypatch, sources=[source(inputs)])
    logs: list[str] = []
    for args in (["--dry-run"], []):
        result = CliRunner().invoke(
            app, ["run", "--week", WEEK, "--config", str(env.config), *args]
        )
        assert result.exit_code == 0, result.output
        logs.append(result.output)
    fake = env.fake
    assert len(fake.drafts) == 1

    # Everything that left the pipeline or was written by it
    conn = db.connect(env.db_path)
    review_ids = {review.review_id for review in db.iter_reviews(conn)}
    corpus: dict[str, str] = {
        f"db:review:{rid[:8]}": text for rid, text in db.iter_review_texts(conn)
    }
    for taxonomy in db.list_taxonomies(conn):
        corpus[f"db:taxonomy:{taxonomy.version}"] = json.dumps(
            [t.model_dump() for t in taxonomy.themes], ensure_ascii=False
        )
    conn.close()
    for path in sorted((env.tmp_path / "runs").rglob("*")):
        if path.is_file():
            corpus[f"runs:{path.relative_to(env.tmp_path)}"] = path.read_text(encoding="utf-8")
    for n, call in enumerate(fake.calls):
        for k, text in enumerate(strings(call.args)):
            corpus[f"mcp:{n}:{call.tool}:{k}"] = text
    corpus["logs"] = "\n".join(logs)
    assert len(corpus) > 20

    # The pipeline's own identifiers, and nothing else
    manifest = json.loads((env.runs / WEEK / "manifest.json").read_text(encoding="utf-8"))
    shas = {s["sha"] for s in manifest["delivery"]["sections"]}
    request_ids = {
        part
        for call in fake.calls
        for part in str(call.args.get("client_request_id", "")).split(":")
    }
    allowed = review_ids | shas | {s[:12] for s in shas} | request_ids | {fake.doc_id, RECIPIENT}
    allowed |= set(re.split(r"[/_-]", str(env.tmp_path.resolve())))
    doc_urls = (fake.url, "https://docs.google.com/document/d/<PULSE_DOC_ID>")

    leaks = []
    for where, text in corpus.items():
        for hit in scan(text):
            matched = text[hit.start : hit.end].rstrip("\",'\\")
            if matched in allowed or matched.startswith(doc_urls):
                continue
            leaks.append(f"{where}: {hit.kind} {matched[:40]!r}")
    assert leaks == [], "\n".join(leaks)

    # The raw values themselves appear nowhere
    for where, text in corpus.items():
        for secret in RAW_SECRETS:
            assert secret not in text, f"{secret!r} in {where}"
        if not where.startswith("db:"):
            assert AGENT_NAME not in text, f"{AGENT_NAME!r} in {where}"

    # The review naming an agent was classified as mentioning a person and never quoted
    pulse = json.loads((env.runs / WEEK / "pulse.json").read_text(encoding="utf-8"))
    assert not any("agent named" in q["quote"] for q in pulse["quotes"])
    quoted_ids = {q["review_id"] for q in pulse["quotes"]}
    conn = db.connect(env.db_path)
    flagged = {c.review_id for c in db.classifications_for(conn, "v1") if c.mentions_person}
    redacted = {r.review_id for r in db.iter_reviews(conn) if r.has_redaction}
    conn.close()
    assert len(flagged) == 1 and quoted_ids.isdisjoint(flagged)
    assert len(redacted) == len(PII_REVIEWS) and quoted_ids.isdisjoint(redacted)
    assert re.search(r"\[(EMAIL|PHONE|PAN|AADHAAR|UPI|ID)\]", " ".join(corpus.values()))
