"""Phase 5: aggregation (T5.1) and quotes (T5.2–T5.4). Synthetic data only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pulse.analysis.aggregate import aggregate, rank, trend
from pulse.analysis.quotes import (
    ThemePool,
    candidate_pool,
    find_span,
    first_sentence,
    select_quotes,
    verify,
)
from pulse.llm.client import CallResult, LLMInvalidOutput, Usage
from pulse.llm.outputs import QuoteChoice, QuoteChoices
from pulse.llm.prompts import load_prompt
from pulse.models import Classification, Review, Taxonomy, Theme, ThemeStats
from pulse.timeutil import parse_iso_week, window_for
from tests.conftest import REPO_ROOT

WEEK = "2026-W41"
WINDOW = window_for(WEEK, 12)
LATEST = parse_iso_week(WEEK)
IN_LATEST = LATEST[0] + timedelta(days=2)
EARLY = WINDOW[0] + timedelta(days=3)
NOW = datetime(2026, 10, 12, tzinfo=UTC)

TAXONOMY = Taxonomy(
    version="v1",
    created_at=NOW,
    approved=True,
    themes=[Theme(id=t, name=t.title(), definition=f"{t} things") for t in "abcd"],
)


def review(
    rid: str,
    *,
    rating: int = 1,
    when: datetime = EARLY,
    text: str | None = None,
    thumbs: int = 0,
    redacted: bool = False,
) -> Review:
    body = text or f"Review {rid} says the app has a real problem that needs fixing soon"
    return Review(
        review_id=rid,
        store="play_store",
        rating=rating,
        text=body,
        review_date=when,
        thumbs_up=thumbs,
        script="latin",
        word_count=len(body.split()),
        has_redaction=redacted,
        ingested_at=NOW,
    )


def label(
    rid: str, theme: str, sentiment: str = "negative", person: bool = False, status: str = "ok"
) -> Classification:
    if status != "ok":
        return Classification(
            review_id=rid, taxonomy_version="v1", status="unclassified", classified_at=NOW
        )
    return Classification(
        review_id=rid,
        taxonomy_version="v1",
        status="ok",
        theme_id=theme,
        sentiment=sentiment,
        mentions_person=person,
        classified_at=NOW,
    )  # type: ignore[arg-type]


def stats_for(reviews: list[Review], labels: dict[str, Classification], **kw: Any) -> Any:
    options: dict[str, Any] = {
        "week": WEEK,
        "window": WINDOW,
        "latest": LATEST,
        "weeks": 12,
        "min_trend_count": 10,
    }
    options.update(kw)
    return aggregate(reviews, labels, TAXONOMY, **options)


# --- T5.1 ----------------------------------------------------------------------------------


def build(
    spec: dict[str, list[tuple[int, datetime]]],
) -> tuple[list[Review], dict[str, Classification]]:
    reviews, labels = [], {}
    for theme, items in spec.items():
        for i, (rating, when) in enumerate(items):
            rid = f"{theme}{i}"
            reviews.append(review(rid, rating=rating, when=when))
            labels[rid] = label(rid, theme, "negative" if rating <= 2 else "positive")
    return reviews, labels


def test_shares_ratings_and_ranking_with_tie() -> None:
    reviews, labels = build(
        {
            "a": [(1, EARLY)] * 4,
            "b": [(5, EARLY)] * 4,  # ties with a on count; a has the lower rating, so ranks first
            "c": [(2, EARLY)] * 2,
            "other": [(3, EARLY)] * 2,
        }
    )
    reviews.append(review("u1"))
    labels["u1"] = label("u1", "a", status="unclassified")
    stats = stats_for(reviews, labels)
    assert stats.total_reviews == 13 and stats.classified_n == 12
    assert stats.top_theme_ids == ["a", "b", "c"]
    by = {t.theme_id: t for t in stats.themes}
    assert by["a"].share == pytest.approx(4 / 12, abs=1e-4)
    assert by["a"].avg_rating == 1 and by["a"].pct_negative == 1.0
    assert stats.other_share == pytest.approx(2 / 12, abs=1e-4)
    assert sum(t.share for t in stats.themes) + stats.other_share == pytest.approx(1.0, abs=1e-3)
    assert by["d"].count == 0 and "d" not in stats.top_theme_ids


def test_trend_markers_and_suppression() -> None:
    assert trend(latest_count=30, weekly_avg=20, min_count=10) == "up"
    assert trend(latest_count=12, weekly_avg=20, min_count=10) == "down"
    assert trend(latest_count=20, weekly_avg=20, min_count=10) == "flat"
    assert trend(latest_count=9, weekly_avg=2, min_count=10) is None


def test_theme_with_zero_latest_week_reviews() -> None:
    reviews, labels = build({"a": [(1, EARLY)] * 24, "b": [(1, IN_LATEST)] * 12})
    by = {t.theme_id: t for t in stats_for(reviews, labels).themes}
    assert by["a"].latest_week_count == 0 and by["a"].trend is None
    assert by["b"].latest_week_count == 12 and by["b"].trend == "up"
    assert by["b"].weekly_avg == 1.0


def test_latest_week_ranking_scope() -> None:
    reviews, labels = build(
        {"a": [(1, EARLY)] * 10, "b": [(1, IN_LATEST)] * 3, "c": [(1, IN_LATEST)] * 1}
    )
    assert stats_for(reviews, labels).top_theme_ids == ["a", "b", "c"]
    latest = stats_for(reviews, labels, ranking_scope="latest_week")
    assert latest.top_theme_ids == ["b", "c", "a"]


def test_rank_is_stable_on_full_ties() -> None:
    t = [
        ThemeStats(
            theme_id=x,
            name=x,
            count=1,
            share=0.5,
            avg_rating=2,
            pct_negative=1,
            latest_week_count=0,
            weekly_avg=0.1,
        )
        for x in ("z", "a")
    ]
    assert [s.theme_id for s in rank(t)] == ["a", "z"]


# --- T5.2 ----------------------------------------------------------------------------------


def test_pool_excludes_redacted_person_and_minority_sentiment() -> None:
    reviews = [
        review("ok1", when=IN_LATEST),
        review("red", when=IN_LATEST, redacted=True),
        review("tok", when=IN_LATEST, text="call me at [PHONE] because my withdrawal is stuck now"),
        review("person", when=IN_LATEST),
        review("happy", rating=5, when=IN_LATEST),
        review("long", when=IN_LATEST, text=" ".join(["word"] * 70)),
    ]
    labels = {r.review_id: label(r.review_id, "a") for r in reviews}
    labels["person"] = label("person", "a", person=True)
    labels["happy"] = label("happy", "a", sentiment="positive")
    pool = candidate_pool("a", reviews, labels, latest=LATEST, max_words=60, limit=15)
    assert [r.review_id for r in pool] == ["ok1"]


def test_pool_prefers_latest_week_then_falls_back_to_window() -> None:
    reviews = [
        review("old_popular", thumbs=50),
        review("new", when=IN_LATEST),
        review("old", thumbs=1),
    ]
    labels = {r.review_id: label(r.review_id, "a") for r in reviews}
    pool = candidate_pool("a", reviews, labels, latest=LATEST, max_words=60, limit=15)
    assert [r.review_id for r in pool] == ["new", "old_popular", "old"]
    assert len(candidate_pool("a", reviews, labels, latest=LATEST, max_words=60, limit=2)) == 2


# --- T5.4 ----------------------------------------------------------------------------------

SOURCE = "Withdrawl pending  since 4 days!! The app says it's processing.\nWorst experience"


@pytest.mark.parametrize(
    ("quote", "ok"),
    [
        ("Withdrawl pending since 4 days!!", True),  # whitespace collapsed only
        ("The app says it's processing.", True),
        ("Withdrawal pending since 4 days!!", False),  # fixed typo
        ("Withdrawl pending since 5 days!!", False),  # changed word
        ("Withdrawl pending since 4 days…", False),  # added ellipsis
        ("The app says it’s processing.", False),  # curly apostrophe
        ("withdrawl pending since 4 days!!", False),  # case changed
        ("", False),
    ],
)
def test_verbatim_check(quote: str, ok: bool) -> None:
    assert (verify(quote, SOURCE, max_words=30) is not None) is ok


def test_verified_span_comes_from_the_source() -> None:
    assert find_span("days!! The app", SOURCE) == "days!! The app"
    assert verify("pending since 4", "pending  since\n4", 30) == "pending  since\n4"
    assert verify(SOURCE, SOURCE, max_words=5) is None  # too long
    assert verify("call [PHONE] now", "call [PHONE] now", 30) is None  # redaction token


def test_first_sentence() -> None:
    assert first_sentence(SOURCE) == "Withdrawl pending  since 4 days!!"


class QuoteLLM:
    def __init__(self, picks: list[QuoteChoice] | Exception) -> None:
        self.picks = picks

    def call_structured(self, stage: str, system: str, user: str, schema: Any) -> CallResult[Any]:
        if isinstance(self.picks, Exception):
            raise self.picks
        return CallResult(QuoteChoices(picks=self.picks), Usage(model="m", calls=1))


def pools() -> list[ThemePool]:
    return [
        ThemePool(
            TAXONOMY.themes[0],
            [
                review("a1", text="Withdrawals take days. Nobody replies."),
                review("a2", text="Money stuck for a week now. Fix it."),
            ],
        ),
        ThemePool(
            TAXONOMY.themes[1], [review("b1", text="App freezes at 9:15 every day. Useless.")]
        ),
        ThemePool(TAXONOMY.themes[2], [review("c1", text="Brokerage is far too high for me.")]),
    ]


def test_select_quotes_uses_model_picks_and_db_text() -> None:
    llm = QuoteLLM(
        [
            QuoteChoice(theme_id="a", cid="c2", quote="Money  stuck for a week now."),
            QuoteChoice(theme_id="b", cid="c1", quote="App freezes at 9:15 every day."),
            QuoteChoice(theme_id="c", cid="c1", quote="Brokerage is far too high for me."),
        ]
    )
    picks, usage, how = select_quotes(
        pools(), llm, load_prompt("quotes", REPO_ROOT / "prompts"), max_words=30
    )
    assert [p.review_id for p in picks] == ["a2", "b1", "c1"]
    assert picks[0].quote == "Money stuck for a week now."  # the stored text, not the model's
    assert how == {"a": "model", "b": "model", "c": "model"}
    assert usage is not None


def test_bad_pick_falls_back_to_first_sentence() -> None:
    llm = QuoteLLM(
        [
            QuoteChoice(theme_id="a", cid="c1", quote="Withdrawals take ages."),
            QuoteChoice(theme_id="b", cid="c9", quote="unknown candidate"),
        ]
    )
    picks, _, how = select_quotes(
        pools(), llm, load_prompt("quotes", REPO_ROOT / "prompts"), max_words=30
    )
    assert picks[0].quote == "Withdrawals take days." and how["a"] == "fallback"
    assert picks[1].quote == "App freezes at 9:15 every day." and how["b"] == "fallback"
    assert len({p.review_id for p in picks}) == 3


def test_failed_call_still_yields_quotes() -> None:
    picks, usage, how = select_quotes(
        pools(),
        QuoteLLM(LLMInvalidOutput("x")),
        load_prompt("quotes", REPO_ROOT / "prompts"),
        max_words=30,
    )
    assert len(picks) == 3 and set(how.values()) == {"fallback"} and usage is None
