"""Takeaways and actions (T6.1) and the compose → render → validate loop (T6.5).

Claude writes one takeaway per top theme and exactly three actions from the statistics, the
verified quotes and sample reviews. Code renders and validates. If only composer-fixable checks
fail (length, missing takeaway, ungrounded action), the composer gets one retry with the failure
list. Anything else, or a second failure, stops the run before delivery.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pulse.compose.render import RenderedPulse, render
from pulse.config import PulseConfig
from pulse.llm.client import LLMClient, Usage
from pulse.llm.outputs import ComposerOutput
from pulse.llm.prompts import Prompt, format_reviews
from pulse.models import PulseContent, PulseStats, QuotePick, Review, Taxonomy
from pulse.validate.guardrails import ValidationReport, validate


class ValidationFailed(RuntimeError):
    def __init__(self, report: ValidationReport, rendered: RenderedPulse | None) -> None:
        reasons = "; ".join(f"{f.check}: {f.message}" for f in report.failures)
        super().__init__(f"pulse failed validation: {reasons}")
        self.report = report
        self.rendered = rendered


@dataclass(frozen=True)
class PulseResult:
    content: PulseContent
    rendered: RenderedPulse
    report: ValidationReport
    usage: Usage | None
    attempts: int


def composer_input(
    stats: PulseStats,
    taxonomy: Taxonomy,
    quotes: Sequence[QuotePick],
    samples: Mapping[str, Sequence[Review]],
) -> str:
    """The user turn: statistics as JSON, quotes, then sample reviews per top theme."""
    names = {theme.id: theme for theme in taxonomy.themes}
    by_id = {t.theme_id: t for t in stats.themes}
    payload = {
        "week": stats.week,
        "reviews_in_window": stats.total_reviews,
        "average_rating": stats.avg_rating,
        "top_themes": [
            {
                "theme_id": theme_id,
                "name": by_id[theme_id].name,
                "definition": names[theme_id].definition if theme_id in names else "",
                "share_of_reviews_pct": round(by_id[theme_id].share * 100),
                "average_rating": by_id[theme_id].avg_rating,
                "negative_pct": round(by_id[theme_id].pct_negative * 100),
                "reviews_this_week": by_id[theme_id].latest_week_count,
                "trend_vs_window_average": by_id[theme_id].trend or "not enough data",
            }
            for theme_id in stats.top_theme_ids
        ],
        "quotes": [{"theme_id": q.theme_id, "rating": q.rating} for q in quotes],
    }
    parts = ["<stats>", json.dumps(payload, ensure_ascii=False, indent=2), "</stats>"]
    for theme_id in stats.top_theme_ids:
        text, _ = format_reviews(list(samples.get(theme_id, [])))
        parts.append(f'<samples theme_id="{theme_id}">\n{text}\n</samples>')
    return "\n".join(parts)


def compose_pulse(
    stats: PulseStats,
    taxonomy: Taxonomy,
    quotes: Sequence[QuotePick],
    samples: Mapping[str, Sequence[Review]],
    review_texts: Mapping[str, str],
    llm: LLMClient,
    prompt: Prompt,
    *,
    title: str,
    limits: PulseConfig,
    min_words: int,
) -> PulseResult:
    system = prompt.render(
        takeaway_max_words=limits.takeaway_max_words, action_max_words=limits.action_max_words
    )
    base = composer_input(stats, taxonomy, quotes, samples)
    user = base
    usage: Usage | None = None
    for attempt in (1, 2):
        result = llm.call_structured("compose", system, user, ComposerOutput)
        usage = result.usage if usage is None else usage + result.usage
        content = PulseContent(
            week=stats.week,
            title=title,
            takeaways=result.output.takeaways,
            quotes=list(quotes),
            actions=result.output.actions,
        )
        rendered = render(stats, taxonomy, content, min_words=min_words)
        report = validate(
            content,
            stats,
            taxonomy,
            markdown=rendered.markdown,
            text=rendered.text,
            review_texts=review_texts,
            limits=limits,
        )
        if report.passed:
            return PulseResult(content, rendered, report, usage, attempt)
        if attempt == 2 or not report.fixable_only:
            raise ValidationFailed(report, rendered)
        problems = "\n".join(f"- {f.message}" for f in report.failures)
        user = (
            f"{base}\n\nYour previous takeaways and actions failed these checks:\n{problems}\n"
            "Rewrite them so every check passes."
        )
    raise AssertionError("unreachable")
