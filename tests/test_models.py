from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from pulse.models import (
    Action,
    Classification,
    PulseContent,
    PulseStats,
    QuotePick,
    RawReview,
    Review,
    RunManifest,
    RunRecord,
    StageStatus,
    Takeaway,
    Taxonomy,
    Theme,
    ThemeStats,
)

T = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)


def _theme(theme_id: str = "kyc_onboarding") -> Theme:
    return Theme(
        id=theme_id,
        name="KYC & Onboarding",
        definition="Account opening and KYC.",
        includes=["kyc"],
        excludes=["withdrawals"],
    )


EXAMPLES: list[BaseModel] = [
    RawReview(
        store="play_store",
        store_review_id="abc",
        rating=2,
        text="Slow app",
        review_date=T,
        app_version="12.3.0",
        thumbs_up=4,
    ),
    Review(
        review_id="h" * 64,
        store="play_store",
        rating=2,
        text="Slow app",
        review_date=T,
        app_version=None,
        thumbs_up=0,
        script="latin",
        word_count=2,
        has_redaction=False,
        ingested_at=T,
    ),
    _theme(),
    Taxonomy(version="v1", created_at=T, themes=[_theme()], approved=True),
    Classification(
        review_id="h" * 64,
        taxonomy_version="v1",
        status="ok",
        theme_id="kyc_onboarding",
        sentiment="negative",
        mentions_person=False,
        model="claude-opus-5-5",
        prompt_version="1",
        classified_at=T,
    ),
    Classification(
        review_id="h" * 64, taxonomy_version="v1", status="unclassified", classified_at=T
    ),
    ThemeStats(
        theme_id="kyc_onboarding",
        name="KYC",
        count=10,
        share=0.25,
        avg_rating=2.1,
        pct_negative=0.7,
        latest_week_count=3,
        weekly_avg=0.8,
        trend="up",
    ),
    PulseStats(
        week="2026-W41",
        window_start=T,
        window_end=T,
        total_reviews=100,
        avg_rating=3.9,
        classified_n=80,
        other_share=0.1,
        themes=[],
        top_theme_ids=["a", "b", "c"],
    ),
    QuotePick(theme_id="a", review_id="r", quote="Withdrawals take days", rating=1),
    Takeaway(theme_id="a", text="Withdrawal delays dominate complaints"),
    Action(theme_id="a", text="Show withdrawal status in-app"),
    PulseContent(week="2026-W41", title="Pulse", takeaways=[], quotes=[], actions=[]),
    StageStatus(
        status="done",
        started_at=T,
        finished_at=T,
        counts={"new": 3, "share": 0.5},
        flags=["partial_ingest"],
    ),
    RunManifest(
        week="2026-W41",
        window_start=T,
        window_end=T,
        created_at=T,
        updated_at=T,
        stages={"ingest": StageStatus(status="done")},
    ),
    RunRecord(
        week="2026-W41",
        window_start=T,
        window_end=T,
        status="delivered",
        doc_id="d",
        created_at=T,
        updated_at=T,
    ),
]


@pytest.mark.parametrize("model", EXAMPLES, ids=lambda m: type(m).__name__)
def test_json_round_trip(model: BaseModel) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


@pytest.mark.parametrize("field", ["userName", "userImage", "replyContent", "author"])
def test_raw_review_rejects_identity_fields(field: str) -> None:
    data = {
        "store": "play_store",
        "store_review_id": "x",
        "rating": 5,
        "text": "ok",
        "review_date": T,
        field: "Some Person",
    }
    with pytest.raises(ValidationError):
        RawReview.model_validate(data)


def test_raw_review_needs_aware_datetime() -> None:
    with pytest.raises(ValidationError):
        RawReview(
            store="s", store_review_id="x", rating=5, text="ok", review_date=datetime(2026, 10, 6)
        )


@pytest.mark.parametrize("rating", [0, 6])
def test_rating_bounds(rating: int) -> None:
    with pytest.raises(ValidationError):
        RawReview(store="s", store_review_id="x", rating=rating, text="ok", review_date=T)


def test_taxonomy_caps_at_five_themes() -> None:
    themes = [_theme(f"t{i}") for i in range(6)]
    with pytest.raises(ValidationError, match="1–5 themes"):
        Taxonomy(version="v1", created_at=T, themes=themes)


def test_taxonomy_rejects_other_and_duplicates() -> None:
    with pytest.raises(ValidationError, match="bucket"):
        Taxonomy(version="v1", created_at=T, themes=[_theme("other")])
    with pytest.raises(ValidationError, match="unique"):
        Taxonomy(version="v1", created_at=T, themes=[_theme("a"), _theme("a")])


def test_ok_classification_needs_labels() -> None:
    with pytest.raises(ValidationError):
        Classification(review_id="r", taxonomy_version="v1", status="ok", classified_at=T)


def test_manifest_stage_transitions() -> None:
    manifest = RunManifest(week="2026-W41", window_start=T, window_end=T)
    manifest.start("ingest")
    assert manifest.stages["ingest"].status == "running"
    manifest.finish("ingest", counts={"new": 5})
    assert manifest.stages["ingest"].status == "done"
    assert manifest.stages["ingest"].counts == {"new": 5}
    manifest.fail("classify", "boom")
    assert manifest.stages["classify"].status == "failed"
    assert manifest.stages["classify"].error == "boom"
