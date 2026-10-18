"""Domain models (T1.2).

`RawReview` is the only shape an ingest adapter can produce. It has no field for usernames,
avatars, reviewer IDs or developer replies, and rejects unknown fields, so identity data can't
reach the pipeline by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Script = Literal["latin", "devanagari", "other"]
Sentiment = Literal["positive", "neutral", "negative"]
ClassificationStatus = Literal["ok", "unclassified"]
Trend = Literal["up", "down", "flat"]
StageState = Literal["pending", "running", "done", "failed"]

OTHER_THEME_ID = "other"
MAX_THEMES = 5


def utc_now() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RawReview(_Model):
    """A review as fetched, limited to allowlisted fields (arch §4.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store: str
    store_review_id: str
    rating: int = Field(ge=1, le=5)
    text: str
    review_date: AwareDatetime
    app_version: str | None = None
    thumbs_up: int = Field(0, ge=0)


class Review(_Model):
    """A scrubbed, normalised review as stored (arch §4.3)."""

    review_id: str
    store: str
    rating: int = Field(ge=1, le=5)
    text: str
    review_date: AwareDatetime
    app_version: str | None = None
    thumbs_up: int = Field(0, ge=0)
    script: Script
    word_count: int = Field(ge=0)
    has_redaction: bool
    ingested_at: AwareDatetime


class Theme(_Model):
    id: str
    name: str
    definition: str
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)


class Taxonomy(_Model):
    version: str
    created_at: AwareDatetime
    themes: list[Theme]
    approved: bool = False

    @model_validator(mode="after")
    def _check_themes(self) -> Taxonomy:
        if not 1 <= len(self.themes) <= MAX_THEMES:
            raise ValueError(f"a taxonomy needs 1–{MAX_THEMES} themes, got {len(self.themes)}")
        ids = [theme.id for theme in self.themes]
        if len(set(ids)) != len(ids):
            raise ValueError("theme ids must be unique")
        if OTHER_THEME_ID in ids:
            raise ValueError(f"'{OTHER_THEME_ID}' is a bucket added by code, not a theme")
        return self

    def theme_ids(self) -> set[str]:
        return {theme.id for theme in self.themes}


class Classification(_Model):
    review_id: str
    taxonomy_version: str
    status: ClassificationStatus
    theme_id: str | None = None
    sentiment: Sentiment | None = None
    mentions_person: bool | None = None
    model: str | None = None
    prompt_version: str | None = None
    classified_at: AwareDatetime

    @model_validator(mode="after")
    def _ok_needs_labels(self) -> Classification:
        if self.status == "ok" and (
            self.theme_id is None or self.sentiment is None or self.mentions_person is None
        ):
            raise ValueError("an 'ok' classification needs theme_id, sentiment and mentions_person")
        return self


class ThemeStats(_Model):
    theme_id: str
    name: str
    count: int = Field(ge=0)
    share: float = Field(ge=0, le=1)
    avg_rating: float | None = None
    pct_negative: float = Field(ge=0, le=1)
    latest_week_count: int = Field(ge=0)
    weekly_avg: float = Field(ge=0)
    trend: Trend | None = None


class PulseStats(_Model):
    week: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    total_reviews: int = Field(ge=0)
    avg_rating: float | None = None
    classified_n: int = Field(ge=0)
    other_share: float = Field(ge=0, le=1)
    themes: list[ThemeStats]
    top_theme_ids: list[str]


class QuotePick(_Model):
    theme_id: str
    review_id: str
    quote: str
    rating: int = Field(ge=1, le=5)


class Takeaway(_Model):
    theme_id: str
    text: str


class Action(_Model):
    theme_id: str
    text: str


class PulseContent(_Model):
    week: str
    title: str
    takeaways: list[Takeaway]
    quotes: list[QuotePick]
    actions: list[Action]


class StageStatus(_Model):
    status: StageState = "pending"
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    counts: dict[str, int | float] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    batch_id: str | None = None
    flags: list[str] = Field(default_factory=list)
    error: str | None = None


class PartResult(_Model):
    """One appended part of a Doc section, as the server reported it."""

    start_index: int
    end_index: int
    revision_id: str
    done_at: AwareDatetime = Field(default_factory=utc_now)


class DocSectionState(_Model):
    """Progress of one week's section in the running Doc (T7.2).

    `sha` covers the whole section, so a changed pulse gets new `client_request_id`s.
    """

    title: str
    sha: str
    parts: dict[str, PartResult] = Field(default_factory=dict)
    complete: bool = False


class DeliveryState(_Model):
    """Delivery progress for a week. Holds IDs and positions only, never Doc or email text."""

    doc_id: str | None = None
    doc_url: str | None = None
    sections: list[DocSectionState] = Field(default_factory=list)
    draft_id: str | None = None
    message_id: str | None = None

    @property
    def section(self) -> DocSectionState | None:
        """The latest section (a `--force` re-delivery appends another one)."""
        return self.sections[-1] if self.sections else None


class RunManifest(_Model):
    """Per-week run record written to `runs/<week>/manifest.json` (T1.5)."""

    week: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    stages: dict[str, StageStatus] = Field(default_factory=dict)
    delivery: DeliveryState = Field(default_factory=DeliveryState)

    def stage(self, name: str) -> StageStatus:
        return self.stages.setdefault(name, StageStatus())

    def start(self, name: str) -> StageStatus:
        stage = StageStatus(status="running", started_at=utc_now())
        self.stages[name] = stage
        return stage

    def finish(self, name: str, counts: dict[str, int | float] | None = None) -> StageStatus:
        stage = self.stage(name)
        stage.status = "done"
        stage.finished_at = utc_now()
        if counts:
            stage.counts.update(counts)
        return stage

    def fail(self, name: str, error: str) -> StageStatus:
        stage = self.stage(name)
        stage.status = "failed"
        stage.finished_at = utc_now()
        stage.error = error
        return stage


class RunRecord(_Model):
    """A row of the `runs` table (arch §4.3)."""

    week: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    taxonomy_version: str | None = None
    status: str
    stats_json: str | None = None
    pulse_json: str | None = None
    doc_id: str | None = None
    doc_url: str | None = None
    doc_section_start_index: int | None = None
    doc_section_end_index: int | None = None
    doc_revision_id: str | None = None
    draft_id: str | None = None
    draft_message_id: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
