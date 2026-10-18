"""Phase 6: word counter, rendering, guardrails and the re-compose loop."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from pulse.compose.composer import ValidationFailed, compose_pulse
from pulse.compose.render import render, title_for
from pulse.config import PulseConfig
from pulse.llm.client import CallResult, Usage
from pulse.llm.outputs import ComposerOutput
from pulse.llm.prompts import load_prompt
from pulse.models import (
    Action,
    PulseContent,
    PulseStats,
    QuotePick,
    Takeaway,
    Taxonomy,
    Theme,
    ThemeStats,
)
from pulse.timeutil import IST, parse_iso_week, window_for
from pulse.validate.guardrails import validate
from pulse.validate.words import note_word_count
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 10, 12, tzinfo=UTC)
WEEK = "2026-W41"
LIMITS = PulseConfig()
TAXONOMY = Taxonomy(
    version="v1",
    created_at=NOW,
    approved=True,
    themes=[
        Theme(id="pay", name="Withdrawals & Payments", definition="Money in and out."),
        Theme(id="app", name="App Performance", definition="Speed and crashes."),
        Theme(id="fees", name="Charges & Brokerage", definition="Fees."),
        Theme(id="kyc", name="KYC & Onboarding", definition="Account opening."),
    ],
)
TEXTS = {
    "r1": "Withdrawal pending since 4 days and nobody replies to my ticket at all",
    "r2": "App hangs every morning at market open, I lose money every time",
    "r3": "brokrage is too high for small investors like me",
}


def stats() -> PulseStats:
    start, end = window_for(WEEK, 12)
    themes = [
        ThemeStats(
            theme_id="pay",
            name="Withdrawals & Payments",
            count=500,
            share=0.34,
            avg_rating=1.4,
            pct_negative=0.9,
            latest_week_count=60,
            weekly_avg=41.7,
            trend="up",
        ),
        ThemeStats(
            theme_id="app",
            name="App Performance",
            count=400,
            share=0.27,
            avg_rating=1.9,
            pct_negative=0.8,
            latest_week_count=33,
            weekly_avg=33.3,
            trend="flat",
        ),
        ThemeStats(
            theme_id="fees",
            name="Charges & Brokerage",
            count=200,
            share=0.14,
            avg_rating=2.5,
            pct_negative=0.6,
            latest_week_count=5,
            weekly_avg=16.7,
        ),
        ThemeStats(
            theme_id="kyc",
            name="KYC & Onboarding",
            count=100,
            share=0.07,
            avg_rating=2.0,
            pct_negative=0.7,
            latest_week_count=8,
            weekly_avg=8.3,
        ),
    ]
    return PulseStats(
        week=WEEK,
        window_start=start,
        window_end=end,
        total_reviews=1534,
        avg_rating=2.89,
        classified_n=1470,
        other_share=0.18,
        themes=themes,
        top_theme_ids=["pay", "app", "fees"],
    )


def quotes() -> list[QuotePick]:
    return [
        QuotePick(
            theme_id="pay", review_id="r1", quote="Withdrawal pending since 4 days", rating=1
        ),
        QuotePick(
            theme_id="app", review_id="r2", quote="App hangs every morning at market open", rating=1
        ),
        QuotePick(
            theme_id="fees",
            review_id="r3",
            quote="brokrage is too high for small investors like me",
            rating=3,
        ),
    ]


GOOD = ComposerOutput(
    takeaways=[
        Takeaway(theme_id="pay", text="Withdrawals stall for days with no status shown"),
        Takeaway(theme_id="app", text="App freezes at market open, orders get stuck"),
        Takeaway(theme_id="fees", text="Small investors find brokerage too high"),
    ],
    actions=[
        Action(theme_id="pay", text="Add an in-app withdrawal tracker with expected credit time"),
        Action(theme_id="app", text="Load-test order placement for the 9:15 market-open spike"),
        Action(theme_id="fees", text="Show a per-order charges breakdown before confirming"),
    ],
)


def content(
    output: ComposerOutput = GOOD, quote_list: list[QuotePick] | None = None
) -> PulseContent:
    return PulseContent(
        week=WEEK,
        title="Groww Review Pulse — Week of 05 Oct 2026",
        takeaways=output.takeaways,
        quotes=quote_list or quotes(),
        actions=output.actions,
    )


def check(
    c: PulseContent,
    s: PulseStats | None = None,
    tax: Taxonomy = TAXONOMY,
    texts: dict[str, str] | None = None,
) -> list[str]:
    s = s or stats()
    rendered = render(s, tax, c, min_words=9)
    report = validate(
        c,
        s,
        tax,
        markdown=rendered.markdown,
        text=rendered.text,
        review_texts=TEXTS if texts is None else texts,
        limits=LIMITS,
    )
    return [f.check for f in report.failures]


# --- T6.3 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("• slow app — ★ ▲", 2),
        ("user-friendly app", 2),
        ("Great 👍 app", 2),
        ("1. Add a tracker", 4),
        ("Note body\nFull pulse: https://docs.google.com/x", 2),
    ],
)
def test_note_word_count(text: str, words: int) -> None:
    assert note_word_count(text) == words


# --- T6.2 ----------------------------------------------------------------------------------


def test_render_layout_and_determinism() -> None:
    first = render(stats(), TAXONOMY, content(), min_words=9)
    assert first == render(stats(), TAXONOMY, content(), min_words=9)
    lines = first.text.splitlines()
    assert lines[0] == "Groww Review Pulse — Week of 05 Oct 2026"
    assert lines[1] == (
        "Source: 1,534 English Play Store reviews (9+ words), 20 Jul – 11 Oct 2026 | Avg 2.9★"
    )
    assert "1. Withdrawals & Payments — 34% of reviews ▲: Withdrawals stall" in first.text
    assert "2. App Performance — 27% of reviews ▬: " in first.text
    assert "3. Charges & Brokerage — 14% of reviews: " in first.text  # no trend: too few
    assert '• "Withdrawal pending since 4 days" — 1★, Withdrawals & Payments' in first.text
    assert first.markdown.startswith("# Groww Review Pulse — Week of 05 Oct 2026\n")
    assert "## Action ideas" in first.markdown
    assert note_word_count(first.text) <= 250


def test_title_uses_week_start_in_ist() -> None:
    template = "Groww Review Pulse — Week of {week_start:%d %b %Y}"
    assert title_for(template, parse_iso_week(WEEK)[0].astimezone(UTC)) == (
        "Groww Review Pulse — Week of 05 Oct 2026"
    )
    assert parse_iso_week(WEEK)[0].tzinfo == IST


# --- T6.4: one failing case per check, plus a passing golden case ---------------------------


def test_golden_case_passes() -> None:
    assert check(content()) == []


def test_structure_failure() -> None:
    two = GOOD.model_copy(update={"takeaways": GOOD.takeaways[:2]})
    assert "structure" in check(content(two))


def test_taxonomy_failure() -> None:
    s = stats().model_copy(update={"top_theme_ids": ["pay", "app", "ghost"]})
    assert "taxonomy" in check(content(), s)


def test_verbatim_failure() -> None:
    edited = quotes()
    edited[0] = edited[0].model_copy(update={"quote": "Withdrawal pending for 4 days"})
    assert "verbatim" in check(content(quote_list=edited))


def test_grounding_failure() -> None:
    actions = [*GOOD.actions[:2], Action(theme_id="kyc", text="Fix KYC")]
    assert "grounding" in check(content(GOOD.model_copy(update={"actions": actions})))


def test_length_failure() -> None:
    wordy = [Takeaway(theme_id=t.theme_id, text=" ".join(["word"] * 40)) for t in GOOD.takeaways]
    failures = check(content(GOOD.model_copy(update={"takeaways": wordy})))
    assert failures.count("length") >= 3


def test_privacy_failure() -> None:
    texts = {**TEXTS, "r1": "Withdrawal pending since 4 days, call 9876543210"}
    leaked = quotes()
    leaked[0] = leaked[0].model_copy(update={"quote": "call 9876543210"})
    assert "privacy" in check(content(quote_list=leaked), texts=texts)


# --- T6.5 re-compose loop ------------------------------------------------------------------


class Composer:
    def __init__(self, *outputs: ComposerOutput) -> None:
        self.outputs = list(outputs)
        self.users: list[str] = []

    def call_structured(self, stage: str, system: str, user: str, schema: Any) -> CallResult[Any]:
        self.users.append(user)
        return CallResult(self.outputs.pop(0), Usage(model="m", calls=1))


def run(llm: Composer, quote_list: list[QuotePick] | None = None) -> Any:
    return compose_pulse(
        stats(),
        TAXONOMY,
        quote_list or quotes(),
        {},
        TEXTS,
        llm,
        load_prompt("compose", REPO_ROOT / "prompts"),
        title="Groww Review Pulse — Week of 05 Oct 2026",
        limits=LIMITS,
        min_words=9,
    )


def test_recompose_fixes_word_limit() -> None:
    long_actions = [a.model_copy(update={"text": " ".join(["very"] * 25)}) for a in GOOD.actions]
    llm = Composer(GOOD.model_copy(update={"actions": long_actions}), GOOD)
    result = run(llm)
    assert result.attempts == 2 and result.report.passed
    assert "failed these checks" in llm.users[1] and "exceeds 20 words" in llm.users[1]


def test_second_failure_stops_the_run() -> None:
    long_actions = [a.model_copy(update={"text": " ".join(["very"] * 25)}) for a in GOOD.actions]
    bad = GOOD.model_copy(update={"actions": long_actions})
    with pytest.raises(ValidationFailed):
        run(Composer(bad, bad))


def test_unfixable_failure_does_not_retry() -> None:
    edited = quotes()
    edited[0] = edited[0].model_copy(update={"quote": "not in the review"})
    llm = Composer(GOOD, GOOD)
    with pytest.raises(ValidationFailed, match="verbatim"):
        run(llm, edited)
    assert len(llm.users) == 1
