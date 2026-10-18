"""Theme statistics and ranking (T5.1). Plain code, no LLM.

Definitions (every number in the pulse comes from here):

- The window holds the **filtered** reviews: English, more than 8 words, no emoji. Shorter and
  non-English reviews are never stored, so `total_reviews` and `avg_rating` describe this set,
  not the app's overall store rating.
- `classified_n`: reviews in the window with a classification of status `ok` (including
  "other"). `unclassified` reviews are excluded from every share.
- `share` = theme count ÷ `classified_n`, rounded to 4 decimals. Theme shares plus
  `other_share` add up to 100%.
- `avg_rating`, `pct_negative`: over the theme's `ok` reviews (`pct_negative` is the share whose
  sentiment is negative).
- `weekly_avg` = theme count ÷ window weeks; `latest_week_count` = theme count in the pulse week.
- Trend: `latest_week_count ÷ weekly_avg` ≥ 1.2 → up (▲), ≤ 0.8 → down (▼), otherwise flat (▬).
  No trend when `latest_week_count < min_trend_count`, because small counts swing wildly.
- Ranking: count descending, then lower average rating first (more pressing), then theme id.
  With `ranking_scope = latest_week`, latest-week count leads instead. "other" is never ranked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pulse.models import (
    OTHER_THEME_ID,
    Classification,
    PulseStats,
    Review,
    Taxonomy,
    ThemeStats,
    Trend,
)

TREND_UP = 1.2
TREND_DOWN = 0.8
TREND_MARKERS: dict[str | None, str] = {"up": "▲", "down": "▼", "flat": "▬", None: ""}


def aggregate(
    reviews: Sequence[Review],
    labels: Mapping[str, Classification],
    taxonomy: Taxonomy,
    *,
    week: str,
    window: tuple[datetime, datetime],
    latest: tuple[datetime, datetime],
    weeks: int,
    min_trend_count: int,
    ranking_scope: Literal["window", "latest_week"] = "window",
    top_n: int = 3,
) -> PulseStats:
    ok = [
        (review, labels[review.review_id])
        for review in reviews
        if review.review_id in labels and labels[review.review_id].status == "ok"
    ]
    classified_n = len(ok)
    themes: list[ThemeStats] = []
    for theme in taxonomy.themes:
        members = [(r, c) for r, c in ok if c.theme_id == theme.id]
        count = len(members)
        latest_count = sum(1 for r, _ in members if latest[0] <= r.review_date <= latest[1])
        weekly_avg = count / weeks
        themes.append(
            ThemeStats(
                theme_id=theme.id,
                name=theme.name,
                count=count,
                share=round(count / classified_n, 4) if classified_n else 0.0,
                avg_rating=round(sum(r.rating for r, _ in members) / count, 2) if count else None,
                pct_negative=(
                    round(sum(1 for _, c in members if c.sentiment == "negative") / count, 4)
                    if count
                    else 0.0
                ),
                latest_week_count=latest_count,
                weekly_avg=round(weekly_avg, 2),
                trend=trend(latest_count, weekly_avg, min_trend_count),
            )
        )
    ranked = rank(themes, ranking_scope)
    other = sum(1 for _, c in ok if c.theme_id == OTHER_THEME_ID)
    return PulseStats(
        week=week,
        window_start=window[0],
        window_end=window[1],
        total_reviews=len(reviews),
        avg_rating=round(sum(r.rating for r in reviews) / len(reviews), 2) if reviews else None,
        classified_n=classified_n,
        other_share=round(other / classified_n, 4) if classified_n else 0.0,
        themes=ranked,
        top_theme_ids=[t.theme_id for t in ranked if t.count > 0][:top_n],
    )


def trend(latest_count: int, weekly_avg: float, min_count: int) -> Trend | None:
    if latest_count < min_count or weekly_avg <= 0:
        return None
    ratio = latest_count / weekly_avg
    if ratio >= TREND_UP:
        return "up"
    if ratio <= TREND_DOWN:
        return "down"
    return "flat"


def rank(
    themes: Sequence[ThemeStats], scope: Literal["window", "latest_week"] = "window"
) -> list[ThemeStats]:
    def key(t: ThemeStats) -> tuple[int, int, float, str]:
        rating = t.avg_rating if t.avg_rating is not None else 6.0
        if scope == "latest_week":
            return (-t.latest_week_count, -t.count, rating, t.theme_id)
        return (-t.count, 0, rating, t.theme_id)

    return sorted(themes, key=key)


def write_stats(runs_dir: Path, stats: PulseStats) -> Path:
    path = runs_dir / stats.week / "stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stats.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
