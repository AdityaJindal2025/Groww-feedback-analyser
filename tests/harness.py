"""Offline harness for whole-pipeline tests: synthetic sources, the fake LLM, the fake MCP server.

Nothing here touches the network, Claude or Google.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from pulse.config import DeliveryConfig
from pulse.deliver.mcp_client import McpClient
from pulse.llm.client import CallResult
from pulse.llm.fake import FakeLLM
from pulse.models import RawReview, Taxonomy, Theme, utc_now
from pulse.store import db
from tests import synthetic
from tests.conftest import REPO_ROOT, SETTINGS_PATH
from tests.fakes.fake_gmail_docs_server import FakeGmailDocsServer

LLM_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "llm"
RECIPIENT = "pulse-team@example.com"


@dataclass
class ListSource:
    """A review source backed by a list (stands in for the Play Store)."""

    reviews: list[RawReview]
    name: str = "play_store"

    def fetch(self, since: datetime) -> Iterator[RawReview]:
        yield from (r for r in self.reviews if r.review_date >= since)


def synthetic_raw() -> list[RawReview]:
    """The synthetic reviews in `tests/synthetic.py`, as a source would return them."""
    return [
        RawReview(
            store="play_store",
            store_review_id=f"gp:{i}",
            rating=r.rating,
            text=r.text,
            review_date=r.review_date,
            app_version=r.app_version,
            thumbs_up=r.thumbs_up,
        )
        for i, r in enumerate(synthetic.reviews())
    ]


def fixture_taxonomy() -> Taxonomy:
    """The approved taxonomy the fake LLM's fixtures label against."""
    proposal = json.loads((LLM_FIXTURES / "discover.json").read_text(encoding="utf-8"))
    return Taxonomy(
        version="v1",
        created_at=utc_now(),
        approved=True,
        themes=[Theme(**theme) for theme in proposal["response"]["themes"]],
    )


def write_settings(tmp_path: Path, **overrides: Any) -> Path:
    """A settings file whose data, runs and themes paths live under `tmp_path`.

    `overrides` are dotted keys, e.g. `{"llm.classify.chunk_size": 20}`.
    """
    data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    data["paths"] = {
        "db": str(tmp_path / "pulse.db"),
        "runs": str(tmp_path / "runs"),
        "prompts": str(REPO_ROOT / "prompts"),
        "themes": str(tmp_path / "themes.yaml"),
    }
    for dotted, value in overrides.items():
        node = data
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def approve_taxonomy(db_path: Path) -> None:
    conn = db.connect(db_path)
    db.save_taxonomy(conn, fixture_taxonomy())
    conn.close()


@dataclass
class Offline:
    """Everything a pipeline test patches, with handles to inspect afterwards."""

    tmp_path: Path
    config: Path
    fake: FakeGmailDocsServer
    sources: list[Any] = field(default_factory=list)

    @property
    def runs(self) -> Path:
        return self.tmp_path / "runs"

    @property
    def db_path(self) -> Path:
        return self.tmp_path / "pulse.db"


def offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sources: list[Any] | None = None,
    fake: FakeGmailDocsServer | None = None,
    approve: bool = True,
    **overrides: Any,
) -> Offline:
    """Patch the pipeline to run fully offline and return the handles."""
    from tests.conftest import SALT

    fake = fake or FakeGmailDocsServer()
    # The CLI loads `.env` from the working directory; never let the repo's real one in.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PULSE_ID_SALT", SALT)
    monkeypatch.setenv("PULSE_LLM", "fake")
    monkeypatch.setenv("PULSE_LLM_FIXTURES", str(LLM_FIXTURES))
    monkeypatch.setenv("PULSE_MCP_TOKEN", "test-token")
    monkeypatch.setenv("PULSE_DOC_ID", fake.doc_id)
    monkeypatch.setenv("PULSE_RECIPIENT", RECIPIENT)
    sources = [ListSource(synthetic_raw())] if sources is None else sources
    monkeypatch.setattr("pulse.stages.build_sources", lambda settings: sources)
    monkeypatch.setattr("pulse.pipeline.check_server", lambda delivery: None)

    def from_settings(delivery: DeliveryConfig) -> McpClient:
        return McpClient(fake.server, tools=delivery.tool_map(), timeout_seconds=5)

    monkeypatch.setattr(McpClient, "from_settings", staticmethod(from_settings))
    config = write_settings(tmp_path, **overrides)
    if approve:
        approve_taxonomy(tmp_path / "pulse.db")
    return Offline(tmp_path, config, fake, sources)


@dataclass
class CrashingLLM:
    """The fake LLM, but the n-th classify call raises (a crash mid-classification)."""

    inner: FakeLLM
    crash_on_classify_call: int | None = None
    classify_calls: int = 0

    def call_structured(self, stage: str, system: str, user: str, schema: Any) -> CallResult[Any]:
        if stage == "classify":
            self.classify_calls += 1
            if self.classify_calls == self.crash_on_classify_call:
                raise KeyboardInterrupt("simulated kill during classify")
        return self.inner.call_structured(stage, system, user, schema)
