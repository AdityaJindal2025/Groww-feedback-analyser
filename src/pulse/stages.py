"""Pipeline stages for Phases 2 and 4–6, wired to the store, settings and run manifest.

Each function runs one step for a week and records it in `runs/<week>/manifest.json`. The CLI
exposes them as stage commands; Phase 8's `pulse run` chains them.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pulse.analysis import taxonomy as taxonomies
from pulse.analysis.aggregate import aggregate, write_stats
from pulse.analysis.classifier import ClassifyReport, classify_reviews
from pulse.analysis.quotes import ThemePool, candidate_pool, select_quotes
from pulse.compose.composer import PulseResult, ValidationFailed, compose_pulse
from pulse.compose.render import title_for
from pulse.config import Settings
from pulse.ingest.base import ReviewSource
from pulse.ingest.service import IngestReport, build_sources, ingest, scan_store
from pulse.llm.client import LLMClient, Usage, record_usage
from pulse.llm.outputs import TaxonomyProposal
from pulse.llm.prompts import format_reviews, load_prompt
from pulse.manifest import open_manifest, save_manifest
from pulse.models import OTHER_THEME_ID, Classification, PulseStats, Review, Taxonomy
from pulse.store import db
from pulse.timeutil import parse_iso_week, window_for
from pulse.validate.guardrails import write_report

Span = tuple[datetime, datetime]


def _window(settings: Settings, week: str) -> tuple[Span, Span]:
    """(the ranking window, the pulse week itself)."""
    return window_for(week, settings.window.weeks), parse_iso_week(week)


# --- Phase 2 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestResult:
    report: IngestReport
    window: Span
    in_window: list[Review]
    stored_total: int
    privacy_hits: int


class IngestError(RuntimeError):
    """No sources are enabled, or the privacy scan of the store found matches."""


def ingest_window(
    conn: sqlite3.Connection,
    settings: Settings,
    week: str,
    *,
    salt: str,
    weeks: int | None = None,
    sources: list[ReviewSource] | None = None,
) -> IngestResult:
    """Fetch, scrub, filter and store the window ending `week`; scan the store afterwards.

    A privacy-scan match marks the stage failed; the caller decides how to report it.
    """
    start, end = window_for(week, weeks or settings.window.weeks)
    sources = build_sources(settings) if sources is None else sources
    if not sources:
        raise IngestError("No review sources are enabled in sources.*")
    runs_dir = settings.paths.runs
    manifest = open_manifest(runs_dir, week, start, end)
    manifest.start("ingest")
    save_manifest(runs_dir, manifest)
    try:
        report = ingest(conn, sources, start, salt=salt, filters=settings.sources.filters)
        privacy_hits = scan_store(conn)
        in_window = db.reviews_in_window(conn, start, end)
        total = db.count_reviews(conn)
    except Exception as exc:
        manifest.fail("ingest", f"{type(exc).__name__}: {exc}")
        save_manifest(runs_dir, manifest)
        raise
    stage = manifest.finish(
        "ingest",
        counts={
            **report.counts(),
            "in_window": len(in_window),
            "stored_total": total,
            "privacy_hits": privacy_hits,
        },
    )
    if report.partial:
        stage.flags.append("partial_ingest")
    if privacy_hits:
        manifest.fail("ingest", f"privacy scan found matches in {privacy_hits} stored reviews")
    save_manifest(runs_dir, manifest)
    return IngestResult(report, (start, end), in_window, total, privacy_hits)


# --- Phase 4 -------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoverResult:
    taxonomy: Taxonomy
    coverage: ClassifyReport
    reviews_read: int
    usage: Usage


def discover(
    conn: sqlite3.Connection, settings: Settings, llm: LLMClient, week: str
) -> DiscoverResult:
    """Propose a taxonomy from the window's reviews, save it unapproved, check its coverage."""
    (start, end), _ = _window(settings, week)
    reviews = db.reviews_in_window(conn, start, end)
    if not reviews:
        raise taxonomies.TaxonomyError(
            f"no reviews in the window ending {week}; run `pulse ingest`"
        )
    analysis = settings.analysis
    corpus = taxonomies.discovery_reviews(
        reviews,
        max_reviews=analysis.discovery_max_reviews,
        sample_size=analysis.discovery_sample_size,
        seed=analysis.sample_seed,
    )
    prompt = load_prompt("discover", settings.paths.prompts)
    user, _ = format_reviews(corpus)
    result = llm.call_structured(
        "discover", prompt.render(max_themes=analysis.max_themes), user, TaxonomyProposal
    )
    taxonomy = taxonomies.save_proposal(conn, result.output, settings.paths.themes)

    sample = taxonomies.coverage_sample(
        reviews,
        size=analysis.coverage_sample_size,
        min_per_rating=analysis.coverage_min_per_rating,
        seed=analysis.sample_seed,
    )
    coverage, _ = classify_reviews(
        conn,
        sample,
        taxonomy,
        llm,
        load_prompt("classify", settings.paths.prompts),
        chunk_size=settings.llm.classify.chunk_size,
    )
    usage = result.usage + coverage.usage if coverage.usage else result.usage

    manifest = open_manifest(settings.paths.runs, week, start, end)
    stage = manifest.start("discover")
    stage.prompt_versions["discover"] = prompt.version
    record_usage(stage, usage)
    manifest.finish(
        "discover",
        counts={
            "reviews_read": len(corpus),
            "themes": len(taxonomy.themes),
            **{f"coverage.{k}": v for k, v in coverage.counts().items()},
        },
    )
    manifest.stages["discover"].flags.append(f"proposed:{taxonomy.version}")
    save_manifest(settings.paths.runs, manifest)
    return DiscoverResult(taxonomy, coverage, len(corpus), usage)


def classify_window(
    conn: sqlite3.Connection, settings: Settings, llm: LLMClient, week: str
) -> ClassifyReport:
    """Classify every not-yet-classified review in the window with the approved taxonomy."""
    taxonomy = taxonomies.require_active(conn)
    (start, end), _ = _window(settings, week)
    todo = db.unclassified_reviews(
        conn, start, end, taxonomy.version, settings.analysis.min_words_to_classify
    )
    prompt = load_prompt("classify", settings.paths.prompts)
    manifest = open_manifest(settings.paths.runs, week, start, end)
    stage = manifest.start("classify")
    stage.prompt_versions["classify"] = prompt.version
    save_manifest(settings.paths.runs, manifest)
    try:
        report, _ = classify_reviews(
            conn, todo, taxonomy, llm, prompt, chunk_size=settings.llm.classify.chunk_size
        )
    except Exception as exc:
        manifest.fail("classify", f"{type(exc).__name__}: {exc}")
        save_manifest(settings.paths.runs, manifest)
        raise
    # Shares over the whole window, not just this run's new reviews
    labels = db.labels_for(conn, taxonomy.version)
    window_ids = {r.review_id for r in db.reviews_in_window(conn, start, end)}
    ok = [labels[i] for i in window_ids if i in labels and labels[i].status == "ok"]
    other = sum(1 for c in ok if c.theme_id == OTHER_THEME_ID) / len(ok) if ok else 0.0
    if report.usage is not None:
        record_usage(stage, report.usage)
    manifest.finish(
        "classify",
        counts={
            **report.counts(),
            "window_classified": len(ok),
            "window_other_share": round(other, 4),
        },
    )
    stage.models["taxonomy"] = taxonomy.version
    if other > settings.analysis.other_share_alert:
        stage.flags.append("taxonomy_review_needed")
    save_manifest(settings.paths.runs, manifest)
    return report


# --- Phases 5 and 6 ------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildResult:
    stats: PulseStats
    pulse: PulseResult
    quote_sources: dict[str, str]
    files: dict[str, Path]


def build_pulse(
    conn: sqlite3.Connection, settings: Settings, llm: LLMClient, week: str
) -> BuildResult:
    """Aggregate → quotes → compose → validate; write the week's pulse files."""
    taxonomy = taxonomies.require_active(conn)
    (start, end), latest = _window(settings, week)
    runs = settings.paths.runs
    out = runs / week
    reviews = db.reviews_in_window(conn, start, end)
    labels = db.labels_for(conn, taxonomy.version)
    manifest = open_manifest(runs, week, start, end)

    # Aggregate (T5.1)
    manifest.start("aggregate")
    stats = aggregate(
        reviews,
        labels,
        taxonomy,
        week=week,
        window=(start, end),
        latest=latest,
        weeks=settings.window.weeks,
        min_trend_count=settings.analysis.min_trend_count,
        ranking_scope=settings.window.ranking_scope,
        top_n=settings.analysis.top_themes,
    )
    write_stats(runs, stats)
    manifest.finish(
        "aggregate",
        counts={
            "reviews": stats.total_reviews,
            "classified": stats.classified_n,
            "other_share": round(stats.other_share, 4),
        },
    )
    unclassified_in_window = sum(1 for r in reviews if r.review_id not in labels)
    if unclassified_in_window:
        manifest.stages["aggregate"].flags.append(f"not_yet_classified:{unclassified_in_window}")
    save_manifest(runs, manifest)

    # Quotes (T5.2–T5.4)
    themes = {t.id: t for t in taxonomy.themes}
    quote_stage = manifest.start("quotes")
    pools = [
        ThemePool(
            themes[theme_id],
            candidate_pool(
                theme_id,
                reviews,
                labels,
                latest=latest,
                max_words=settings.analysis.quote_candidate_max_words,
                limit=settings.analysis.quote_candidates,
            ),
        )
        for theme_id in stats.top_theme_ids
    ]
    quotes_prompt = load_prompt("quotes", settings.paths.prompts)
    quotes, quote_usage, how = select_quotes(
        pools, llm, quotes_prompt, max_words=settings.pulse.quote_max_words
    )
    quote_stage.prompt_versions["quotes"] = quotes_prompt.version
    if quote_usage:
        record_usage(quote_stage, quote_usage)
    manifest.finish(
        "quotes",
        counts={
            "from_model": sum(1 for v in how.values() if v == "model"),
            "from_fallback": sum(1 for v in how.values() if v == "fallback"),
        },
    )
    save_manifest(runs, manifest)

    # Compose + validate (T6.1–T6.5)
    samples = _samples(
        reviews, labels, stats.top_theme_ids, settings.pulse.composer_samples_per_theme
    )
    texts = db.review_texts(conn, [q.review_id for q in quotes])
    compose_prompt = load_prompt("compose", settings.paths.prompts)
    compose_stage = manifest.start("compose")
    compose_stage.prompt_versions["compose"] = compose_prompt.version
    title = title_for(settings.delivery.docs.title, parse_iso_week(week)[0])
    try:
        result = compose_pulse(
            stats,
            taxonomy,
            quotes,
            samples,
            texts,
            llm,
            compose_prompt,
            title=title,
            limits=settings.pulse,
            min_words=settings.sources.filters.min_words,
        )
    except ValidationFailed as exc:
        write_report(runs, week, exc.report)
        if exc.rendered is not None:
            (out / "pulse.md").write_text(exc.rendered.markdown, encoding="utf-8")
            (out / "pulse.txt").write_text(exc.rendered.text, encoding="utf-8")
        manifest.fail("compose", str(exc))
        manifest.fail("validate", "failed_validation")
        save_manifest(runs, manifest)
        raise
    if result.usage:
        record_usage(compose_stage, result.usage)
    manifest.finish("compose", counts={"attempts": result.attempts})
    manifest.start("validate")
    manifest.finish("validate", counts={"word_count": result.report.word_count})
    files = {
        "stats": out / "stats.json",
        "validation": write_report(runs, week, result.report),
        "pulse_json": out / "pulse.json",
        "markdown": out / "pulse.md",
        "text": out / "pulse.txt",
    }
    files["pulse_json"].write_text(
        result.content.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    files["markdown"].write_text(result.rendered.markdown, encoding="utf-8")
    files["text"].write_text(result.rendered.text, encoding="utf-8")
    save_manifest(runs, manifest)
    return BuildResult(stats, result, how, files)


def _samples(
    reviews: list[Review],
    labels: Mapping[str, Classification],
    theme_ids: list[str],
    per_theme: int,
) -> dict[str, list[Review]]:
    """Up to `per_theme` reviews per theme for context: most thumbs-up first, then newest."""
    grouped: dict[str, list[Review]] = defaultdict(list)
    newest_first = sorted(reviews, key=lambda r: r.review_date, reverse=True)
    for review in sorted(newest_first, key=lambda r: -r.thumbs_up):
        label = labels.get(review.review_id)
        if (
            label
            and label.status == "ok"
            and label.theme_id in theme_ids
            and len(grouped[label.theme_id]) < per_theme
        ):
            grouped[label.theme_id].append(review)
    return dict(grouped)
