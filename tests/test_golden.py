"""T9.1 golden end-to-end test: hand-written reviews → `pulse run` → the same bytes every time.

Reviews come from `tests/fixtures/reviews_synthetic.jsonl` through the real CSV/JSONL import
(scrub, filters, hashing); Claude is the fake LLM; Google is the fake MCP server. The pulse
files and the planned MCP calls must equal the checked-in golden files byte for byte.

After an intended change to the pulse's layout or content, regenerate the golden files with
`PULSE_UPDATE_GOLDEN=1 pytest tests/test_golden.py` and review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from pulse.cli import app
from pulse.deliver.mcp_client import ToolCall
from pulse.deliver.service import mask
from pulse.store import db
from tests.conftest import REPO_ROOT
from tests.golden_inputs import FAILING_FILTERS, golden_rows, source
from tests.harness import RECIPIENT, Offline, offline

WEEK = "2026-W41"
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "golden" / WEEK
GOLDEN_FILES = ("pulse.md", "pulse.txt", "delivery_payloads.json")
TOOL_OPS = {
    "gdocs_get_document_info": "docs.info",
    "gdocs_append_text": "docs.append_text",
    "gdocs_append_table": "docs.append_table",
    "gmail_create_draft": "gmail.create_draft",
}


def run(env: Offline, *args: str) -> Result:
    result = CliRunner().invoke(app, ["run", "--week", WEEK, "--config", str(env.config), *args])
    assert result.exit_code == 0, result.output
    return result


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Offline:
    return offline(tmp_path, monkeypatch, sources=[source()])


def test_fixture_shape() -> None:
    rows = golden_rows()
    assert 55 <= len(rows) <= 70
    ratings = [int(str(r["rating"])) for r in rows]
    assert sum(1 for r in ratings if r in (1, 5)) / len(ratings) > 0.6


def test_golden_pulse(env: Offline) -> None:
    run(env, "--dry-run")
    out = env.runs / WEEK
    if os.environ.get("PULSE_UPDATE_GOLDEN"):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for name in GOLDEN_FILES:
            (GOLDEN / name).write_bytes((out / name).read_bytes())
    for name in GOLDEN_FILES:
        assert (out / name).read_bytes() == (GOLDEN / name).read_bytes(), (
            f"{name} differs from tests/fixtures/golden/{WEEK}/{name}; if the change is "
            "intended, regenerate with PULSE_UPDATE_GOLDEN=1"
        )

    # Two real runs: exactly one W41 section in the Doc, one draft, and the calls the server
    # received are the planned ones.
    run(env)
    run(env)
    fake = env.fake
    headings = [e.text for e in fake.elements if e.style == "HEADING_1"]
    assert headings == ["Groww Review Pulse — Week of 05 Oct 2026"]
    assert len(fake.elements) == 8 and len(fake.drafts) == 1 and fake.sent == []
    planned = json.loads((GOLDEN / "delivery_payloads.json").read_text(encoding="utf-8"))
    first_run = fake.calls[: len(planned)]
    received = mask(
        [
            ToolCall(part=p["part"], op=TOOL_OPS[c.tool], args=c.args)
            for p, c in zip(planned, first_run, strict=True)
        ],
        document=fake.doc_id,
        recipient=RECIPIENT,
    )
    assert received == planned
    # The second run was a complete no-op: no Doc read, no writes
    assert len(fake.calls) == len(planned)


def test_filtered_reviews_never_reach_the_pulse(env: Offline) -> None:
    run(env, "--no-deliver")
    conn = db.connect(env.db_path)
    stored = {review.text for review in db.iter_reviews(conn)}
    conn.close()
    rows = golden_rows()
    kept = [str(r["text"]) for r in rows[:-FAILING_FILTERS]]
    dropped = [str(r["text"]) for r in rows[-FAILING_FILTERS:]]
    assert set(kept) == stored
    pulse_files = "".join(
        (env.runs / WEEK / name).read_text(encoding="utf-8") for name in ("pulse.md", "pulse.txt")
    )
    for text in dropped:
        assert text not in stored
        assert text[:30] not in pulse_files
    manifest = json.loads((env.runs / WEEK / "manifest.json").read_text())
    counts = manifest["stages"]["ingest"]["counts"]
    assert counts["fetched"] == len(rows)
    assert counts["filtered.short"] == 2
    assert counts["filtered.emoji"] == 2
    assert counts["filtered.language"] == 2
