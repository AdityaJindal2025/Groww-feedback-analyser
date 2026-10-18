"""Deterministic rendering of the pulse (T6.2): `pulse.md` for the Doc, `pulse.txt` for email.

Layout follows PS §9. The source line names the ingest filter, so readers don't compare the
average rating of these reviews with the store's overall rating.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pulse.analysis.aggregate import TREND_MARKERS
from pulse.models import PulseContent, PulseStats, Taxonomy
from pulse.timeutil import IST

TEMPLATES = Path(__file__).parent / "templates"
_ENV = Environment(
    loader=FileSystemLoader(TEMPLATES),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=False,
    autoescape=False,
)


@dataclass(frozen=True)
class RenderedPulse:
    markdown: str
    text: str


def title_for(template: str, week_start: datetime) -> str:
    """`settings.delivery.docs.title`, e.g. "Groww Review Pulse — Week of {week_start:%d %b %Y}"."""
    return template.format(week_start=week_start.astimezone(IST))


def source_line(stats: PulseStats, min_words: int) -> str:
    start = stats.window_start.astimezone(IST)
    end = stats.window_end.astimezone(IST)
    span = (
        f"{start:%d %b} – {end:%d %b %Y}"
        if start.year == end.year
        else (f"{start:%d %b %Y} – {end:%d %b %Y}")
    )
    avg = f"{stats.avg_rating:.1f}★" if stats.avg_rating is not None else "n/a"
    return (
        f"{stats.total_reviews:,} English Play Store reviews ({min_words}+ words), "
        f"{span} | Avg {avg}"
    )


def view_model(
    stats: PulseStats, taxonomy: Taxonomy, content: PulseContent, *, min_words: int
) -> dict[str, Any]:
    names = {theme.id: theme.name for theme in taxonomy.themes}
    by_id = {t.theme_id: t for t in stats.themes}
    takeaways = {t.theme_id: t.text for t in content.takeaways}
    return {
        "title": content.title,
        "source": source_line(stats, min_words),
        "themes": [
            {
                "name": names.get(theme_id, theme_id),
                "share_pct": round(by_id[theme_id].share * 100),
                "marker": f" {TREND_MARKERS[by_id[theme_id].trend]}"
                if by_id[theme_id].trend
                else "",
                "takeaway": takeaways.get(theme_id, ""),
            }
            for theme_id in stats.top_theme_ids
            if theme_id in by_id
        ],
        "quotes": [
            {"quote": q.quote, "rating": q.rating, "theme_name": names.get(q.theme_id, q.theme_id)}
            for q in content.quotes
        ],
        "actions": [{"text": a.text} for a in content.actions],
    }


def render(
    stats: PulseStats, taxonomy: Taxonomy, content: PulseContent, *, min_words: int
) -> RenderedPulse:
    data = view_model(stats, taxonomy, content, min_words=min_words)
    return RenderedPulse(
        markdown=_ENV.get_template("pulse.md.j2").render(**data),
        text=_ENV.get_template("pulse.txt.j2").render(**data),
    )
