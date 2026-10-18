"""Quote candidates, selection and verbatim checking (T5.2–T5.4).

Claude picks a candidate and an excerpt; code decides whether it may be used. An excerpt passes
only if, after collapsing whitespace, it appears exactly in the stored review (same spelling,
case, punctuation and apostrophes), is within the word limit and has no redaction token. The
text that reaches the pulse is the matching span **of the stored review**, never the model's
string. If the pick fails, code falls back to whole first sentences of the ranked candidates.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pulse.ingest.normalize import word_count
from pulse.llm.client import LLMClient, LLMError, Usage
from pulse.llm.outputs import QuoteChoices
from pulse.llm.prompts import Prompt, format_reviews
from pulse.models import Classification, QuotePick, Review, Theme
from pulse.privacy.scrubber import has_redaction

log = logging.getLogger(__name__)

IDEAL_WORDS = 25
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class QuoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThemePool:
    theme: Theme
    candidates: list[Review]


# --- candidate pool (T5.2) -----------------------------------------------------------------


def candidate_pool(
    theme_id: str,
    reviews: Sequence[Review],
    labels: Mapping[str, Classification],
    *,
    latest: tuple[datetime, datetime],
    max_words: int,
    limit: int,
) -> list[Review]:
    """Quotable reviews for a theme, best first: latest week, then most thumbs-up, then
    closest to 25 words. Only reviews matching the theme's majority sentiment qualify."""
    members = [
        (review, labels[review.review_id])
        for review in reviews
        if (label := labels.get(review.review_id)) is not None
        and label.status == "ok"
        and label.theme_id == theme_id
    ]
    sentiments = Counter(label.sentiment for _, label in members)
    if not sentiments:
        return []
    majority = sorted(sentiments.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    eligible = [
        review
        for review, label in members
        if label.sentiment == majority
        and not label.mentions_person
        and not review.has_redaction
        and not has_redaction(review.text)
        and review.word_count <= max_words
    ]
    eligible.sort(
        key=lambda r: (
            not (latest[0] <= r.review_date <= latest[1]),
            -r.thumbs_up,
            abs(r.word_count - IDEAL_WORDS),
            r.review_id,
        )
    )
    return eligible[:limit]


# --- verbatim check (T5.4) -----------------------------------------------------------------


def find_span(quote: str, source: str) -> str | None:
    """The span of `source` that equals `quote` up to whitespace, or None.

    Only runs of whitespace are treated as equal; every other character must match exactly.
    """
    words = quote.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(word) for word in words)
    match = re.search(pattern, source)
    return match.group(0) if match else None


def verify(quote: str, source: str, max_words: int) -> str | None:
    """The verified span from `source`, or None if the quote can't be used."""
    span = find_span(quote, source)
    if span is None or word_count(span) == 0 or word_count(span) > max_words:
        return None
    if has_redaction(span):
        return None
    return span


def first_sentence(text: str) -> str:
    return _SENTENCE_END.split(text.strip(), maxsplit=1)[0].strip()


# --- selection (T5.3) ----------------------------------------------------------------------


def select_quotes(
    pools: Sequence[ThemePool],
    llm: LLMClient,
    prompt: Prompt,
    *,
    max_words: int,
) -> tuple[list[QuotePick], Usage | None, dict[str, str]]:
    """One verified quote per theme, from different reviews.

    Returns the picks, the LLM usage, and how each quote was chosen (`model` or `fallback`).
    """
    blocks: list[str] = []
    id_maps: dict[str, dict[str, str]] = {}
    by_id: dict[str, Review] = {}
    for pool in pools:
        text, ids = format_reviews(pool.candidates, prefix="c")
        id_maps[pool.theme.id] = ids
        by_id.update({r.review_id: r for r in pool.candidates})
        blocks.append(
            f'<theme id="{pool.theme.id}" name="{pool.theme.name}">\n'
            f"Definition: {pool.theme.definition}\n{text}\n</theme>"
        )
    usage: Usage | None = None
    model_picks: dict[str, tuple[str, str]] = {}
    try:
        result = llm.call_structured(
            "quotes", prompt.render(quote_max_words=max_words), "\n\n".join(blocks), QuoteChoices
        )
        usage = result.usage
        for pick in result.output.picks:
            review_id = id_maps.get(pick.theme_id, {}).get(pick.cid)
            if review_id is not None:
                model_picks[pick.theme_id] = (review_id, pick.quote)
    except LLMError as exc:
        log.warning("quote selection call failed; using fallbacks", extra={"error": str(exc)})

    picks: list[QuotePick] = []
    how: dict[str, str] = {}
    used: set[str] = set()
    for pool in pools:
        theme_id = pool.theme.id
        chosen: tuple[Review, str] | None = None
        if theme_id in model_picks:
            review_id, quote = model_picks[theme_id]
            review = by_id[review_id]
            span = verify(quote, review.text, max_words)
            if span is not None and review_id not in used:
                chosen, how[theme_id] = (review, span), "model"
        if chosen is None:
            for review in pool.candidates:
                if review.review_id in used:
                    continue
                span = verify(first_sentence(review.text), review.text, max_words)
                if span is not None:
                    chosen, how[theme_id] = (review, span), "fallback"
                    break
        if chosen is None:
            raise QuoteError(f"no verifiable quote for theme {theme_id!r}")
        review, span = chosen
        used.add(review.review_id)
        picks.append(
            QuotePick(
                theme_id=theme_id, review_id=review.review_id, quote=span, rating=review.rating
            )
        )
    return picks, usage, how
