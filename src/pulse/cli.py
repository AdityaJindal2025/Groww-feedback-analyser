"""`pulse` command line (Typer).

Exit codes (T8.3): 0 done, 1 unexpected error, 2 validation failed, 3 delivery failed,
4 precondition failed.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from pulse import env, pipeline, stages
from pulse.analysis import taxonomy as taxonomies
from pulse.analysis.quotes import QuoteError
from pulse.compose.composer import ValidationFailed
from pulse.config import DEFAULT_SETTINGS_PATH, ConfigError, Settings, load_settings
from pulse.deliver.mcp_client import DeliveryError
from pulse.deliver.service import deliver_week
from pulse.eval import LABEL_FILE, EvalError, evaluate, export_sheet
from pulse.llm.client import LLMClient, LLMError, has_credentials, make_client
from pulse.logging import configure_logging
from pulse.manifest import load_manifest
from pulse.models import Taxonomy
from pulse.store import db
from pulse.timeutil import (
    last_completed_week,
    parse_iso_week,
    week_of,
    weeks_between,
    window_for,
)

EXIT_VALIDATION = 2
EXIT_DELIVERY = 3
EXIT_PRECONDITION = 4

app = typer.Typer(
    help="Weekly Groww Play Store review pulse.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOption = Annotated[Path, typer.Option("--config", help="Settings file.", show_default=True)]


@app.callback()
def main() -> None:
    env.load_env()
    configure_logging()


def _fail(message: str, code: int) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _load(config: Path) -> Settings:
    try:
        return load_settings(config)
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc


@app.command("ingest")
def ingest_command(
    week: Annotated[
        str | None,
        typer.Option(help="ISO week the window ends with, e.g. 2026-W41. Default: last completed."),
    ] = None,
    weeks: Annotated[
        int | None,
        typer.Option(min=8, max=12, help="Window length in weeks. Default: window.weeks."),
    ] = None,
    config: ConfigOption = DEFAULT_SETTINGS_PATH,
) -> None:
    """Fetch, scrub and store public reviews for the window ending WEEK."""
    settings = _load(config)
    try:
        salt = env.id_salt()
    except env.MissingEnvError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc

    week = week or last_completed_week(datetime.now(UTC))
    try:
        window_for(week, weeks or settings.window.weeks)
    except ValueError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc

    conn = db.connect(settings.paths.db)
    try:
        result = stages.ingest_window(conn, settings, week, salt=salt, weeks=weeks)
    except stages.IngestError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc
    finally:
        conn.close()
    report, in_window, total = result.report, result.in_window, result.stored_total
    privacy_hits = result.privacy_hits
    start, end = result.window

    per_week = Counter(week_of(review.review_date) for review in in_window)
    typer.echo(
        f"Window {week} ({weeks or settings.window.weeks} weeks): "
        f"{start:%d %b %Y} – {end:%d %b %Y} IST"
    )
    filtered = report.filtered
    typer.echo(
        f"Fetched {report.fetched}; dropped {sum(filtered.values())} "
        f"(8 words or fewer {filtered['short']}, emoji {filtered['emoji']}, "
        f"not English {filtered['language']})"
    )
    typer.echo(
        f"Kept: {report.new} new, {report.updated} updated, {report.duplicates} already stored"
    )
    if report.pruned:
        typer.echo(f"Removed {report.pruned} stored reviews that fail the current filters")
    if report.redactions:
        typer.echo(
            "Redactions: "
            + ", ".join(f"{kind} {n}" for kind, n in sorted(report.redactions.items()))
        )
    for name, reason in report.partial.items():
        typer.secho(f"Partial ingest from {name}: {reason}", fg=typer.colors.YELLOW)
    typer.echo(f"In window: {len(in_window)} reviews; stored in total: {total}")
    for label in weeks_between(start, end):
        typer.echo(f"  {label}  {per_week.get(label, 0):>6}")
    if privacy_hits:
        raise _fail(
            f"Privacy scan failed: {privacy_hits} stored reviews still match "
            "personal-data patterns",
            EXIT_VALIDATION,
        )
    typer.echo("Privacy scan: clean")


# --- shared helpers for the LLM stages -------------------------------------------------------

WeekOption = Annotated[
    str | None,
    typer.Option(help="ISO week of the pulse, e.g. 2026-W41. Default: last completed week."),
]


def _week(week: str | None) -> str:
    week = week or last_completed_week(datetime.now(UTC))
    try:
        parse_iso_week(week)
    except ValueError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc
    return week


def _llm(settings: Settings) -> LLMClient:
    import os

    if os.environ.get("PULSE_LLM", "").strip().lower() != "fake" and not has_credentials():
        raise _fail(
            "No Claude credentials: set ANTHROPIC_API_KEY in .env or run `ant auth login`.",
            EXIT_PRECONDITION,
        )
    return make_client(settings.llm)


def _active(conn: sqlite3.Connection) -> Taxonomy:
    try:
        return taxonomies.require_active(conn)
    except taxonomies.TaxonomyError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc


def _themes_table(taxonomy: Taxonomy) -> None:
    for theme in taxonomy.themes:
        typer.echo(f"  {theme.id:<28} {theme.name}")
        typer.echo(f"  {'':<28} {theme.definition}")


# --- themes (T4.2, T4.3) ---------------------------------------------------------------------

themes_app = typer.Typer(help="Discover, list and approve theme taxonomies.", no_args_is_help=True)
app.add_typer(themes_app, name="themes")


@themes_app.command("discover")
def themes_discover(week: WeekOption = None, config: ConfigOption = DEFAULT_SETTINGS_PATH) -> None:
    """Propose a taxonomy from the window's reviews and check how well it covers them."""
    settings = _load(config)
    week = _week(week)
    llm = _llm(settings)
    conn = db.connect(settings.paths.db)
    try:
        result = stages.discover(conn, settings, llm, week)
    except taxonomies.TaxonomyError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc
    finally:
        conn.close()
    taxonomy, coverage = result.taxonomy, result.coverage
    typer.echo(
        f"Proposed taxonomy {taxonomy.version} from {result.reviews_read} reviews "
        f"(not approved yet):\n"
    )
    _themes_table(taxonomy)
    typer.echo(f"\nCoverage on a {coverage.considered}-review stratified sample:")
    for theme_id, n in coverage.by_theme.most_common():
        typer.echo(f"  {theme_id:<28} {n / max(coverage.ok, 1):6.1%}")
    if coverage.unclassified:
        typer.echo(f"  {'(unclassified)':<28} {coverage.unclassified}")
    alert = settings.analysis.other_share_alert
    if coverage.other_share > alert:
        typer.secho(
            f"'other' is {coverage.other_share:.0%} (alert above {alert:.0%}); "
            "consider discovering again.",
            fg=typer.colors.YELLOW,
        )
    typer.echo(
        f"\nEstimated cost: ${result.usage.cost_usd:.2f}. "
        f"Review {settings.paths.themes}, then run `pulse themes approve "
        f"{taxonomy.version}`."
    )


@themes_app.command("approve")
def themes_approve(version: str, config: ConfigOption = DEFAULT_SETTINGS_PATH) -> None:
    """Make VERSION the active taxonomy (the previous one is deactivated)."""
    settings = _load(config)
    conn = db.connect(settings.paths.db)
    try:
        taxonomy = taxonomies.approve(conn, version, settings.paths.themes)
    except taxonomies.TaxonomyError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc
    finally:
        conn.close()
    typer.echo(f"Approved {taxonomy.version}; it is now the active taxonomy.")


@themes_app.command("list")
def themes_list(config: ConfigOption = DEFAULT_SETTINGS_PATH) -> None:
    """List taxonomy versions; the active one is marked."""
    settings = _load(config)
    conn = db.connect(settings.paths.db)
    try:
        versions = db.list_taxonomies(conn)
    finally:
        conn.close()
    if not versions:
        typer.echo("No taxonomies yet. Run `pulse themes discover`.")
    for taxonomy in versions:
        mark = "* active" if taxonomy.approved else ""
        typer.echo(f"{taxonomy.version} ({taxonomy.created_at:%Y-%m-%d}) {mark}")
        _themes_table(taxonomy)


# --- classify (T4.5) -------------------------------------------------------------------------


@app.command("classify")
def classify_command(week: WeekOption = None, config: ConfigOption = DEFAULT_SETTINGS_PATH) -> None:
    """Classify the window's not-yet-classified reviews with the active taxonomy."""
    settings = _load(config)
    week = _week(week)
    conn = db.connect(settings.paths.db)
    try:
        _active(conn)
        llm = _llm(settings)
        report = stages.classify_window(conn, settings, llm, week)
    except LLMError as exc:
        raise _fail(f"{exc}. Re-run to continue; finished chunks are saved.", 1) from exc
    finally:
        conn.close()
    cost = report.usage.cost_usd if report.usage else 0.0
    typer.echo(
        f"Classified {report.ok} reviews ({report.unclassified} unclassified) in "
        f"{report.chunks} calls; estimated cost ${cost:.2f}."
    )
    for theme_id, n in report.by_theme.most_common():
        typer.echo(f"  {theme_id:<28} {n}")


# --- aggregate, quotes, compose, validate (Phases 5 and 6) -----------------------------------


@app.command("compose")
def compose_command(week: WeekOption = None, config: ConfigOption = DEFAULT_SETTINGS_PATH) -> None:
    """Build the week's pulse locally: stats, quotes, takeaways, actions, guardrails."""
    settings = _load(config)
    week = _week(week)
    conn = db.connect(settings.paths.db)
    try:
        _active(conn)
        llm = _llm(settings)
        result = stages.build_pulse(conn, settings, llm, week)
    except (ValidationFailed, QuoteError) as exc:
        raise _fail(
            f"{exc}\nSee {settings.paths.runs / week}/validation.json", EXIT_VALIDATION
        ) from exc
    except LLMError as exc:
        raise _fail(str(exc), 1) from exc
    finally:
        conn.close()
    typer.echo(result.pulse.rendered.text)
    typer.echo(
        f"Words: {result.pulse.report.word_count}/{settings.pulse.max_words}; "
        f"guardrails passed; quotes: "
        + ", ".join(f"{k}={v}" for k, v in result.quote_sources.items())
    )
    typer.echo(f"Written: {', '.join(str(p) for p in result.files.values())}")


# --- deliver (Phase 7) ----------------------------------------------------------------------


@app.command("deliver")
def deliver_command(
    week: WeekOption = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Write runs/<week>/delivery_payloads.json and make no MCP calls.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Append a new '(updated <date>)' section and create a new draft, even if the "
            "week was already delivered.",
        ),
    ] = False,
    config: ConfigOption = DEFAULT_SETTINGS_PATH,
) -> None:
    """Append the week's pulse to the Google Doc, then create a Gmail draft (never sends).

    Safe to re-run: it resumes at the first unfinished Doc part and skips what's done.
    """
    settings = _load(config)
    week = _week(week)
    conn = db.connect(settings.paths.db)
    try:
        report = deliver_week(conn, settings, week, dry_run=dry_run, force=force)
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc
    except DeliveryError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION if exc.precondition else EXIT_DELIVERY) from exc
    finally:
        conn.close()
    if report.dry_run:
        typer.echo(
            f"Dry run: {len(report.calls)} planned MCP calls written to {report.payload_file}. "
            "Nothing was sent."
        )
        return
    doc, draft = report.doc, report.draft
    assert doc is not None and draft is not None
    where = f" (indexes {doc.start_index}–{doc.end_index})" if doc.start_index is not None else ""
    typer.echo(f"Doc section: {doc.status}{where}: {doc.doc_url}")
    typer.echo(
        f"Gmail draft: {draft.status} (draft {draft.draft_id}); review and send it yourself."
    )


# --- run and show (Phase 8) ----------------------------------------------------------------


@app.command("run")
def run_command(
    week: WeekOption = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Build the pulse, write the planned MCP calls to "
            "runs/<week>/delivery_payloads.json, and send nothing.",
        ),
    ] = False,
    no_deliver: Annotated[
        bool, typer.Option("--no-deliver", help="Stop after validation; skip delivery.")
    ] = False,
    from_stage: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Rerun from this stage: " + ", ".join(pipeline.STAGES) + ".",
            show_default=False,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Deliver again even if the week was delivered: a new '(updated <date>)' Doc "
            "section and a new draft.",
        ),
    ] = False,
    config: ConfigOption = DEFAULT_SETTINGS_PATH,
) -> None:
    """Run the weekly pipeline: ingest → classify → compose → validate → deliver.

    Stages already done for the week are skipped, so re-running resumes after a failure and a
    delivered week is a no-op.
    """
    settings = _load(config)
    week = _week(week)
    options = pipeline.RunOptions(
        dry_run=dry_run, no_deliver=no_deliver, force=force, from_stage=from_stage
    )
    conn = db.connect(settings.paths.db)
    try:
        result = pipeline.run(pipeline.RunContext(conn, settings, week, options))
    except (pipeline.PreconditionError, ConfigError) as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc
    except (ValidationFailed, QuoteError) as exc:
        raise _fail(
            f"{exc}\nSee {settings.paths.runs / week}/validation.json", EXIT_VALIDATION
        ) from exc
    except pipeline.PrivacyScanFailed as exc:
        raise _fail(str(exc), EXIT_VALIDATION) from exc
    except DeliveryError as exc:
        raise _fail(
            f"{exc}\nThe pulse is saved; retry with `pulse deliver --week {week}`.",
            EXIT_PRECONDITION if exc.precondition else EXIT_DELIVERY,
        ) from exc
    except LLMError as exc:
        raise _fail(f"{exc}. Re-run `pulse run` to continue; finished work is kept.", 1) from exc
    finally:
        conn.close()
    typer.echo(f"Week {week}:")
    for stage, outcome in result.stages.items():
        typer.echo(f"  {stage:<10} {outcome}")
    report = result.delivery
    if report is not None and report.dry_run:
        typer.echo(f"Dry run: planned MCP calls in {report.payload_file}. Nothing was sent.")
    elif report is not None and report.doc is not None and report.draft is not None:
        typer.echo(f"Doc section: {report.doc.status}: {report.doc.doc_url}")
        typer.echo(f"Gmail draft: {report.draft.status} (draft {report.draft.draft_id})")
    elif "deliver" not in result.stages or result.stages["deliver"] == "skipped":
        typer.echo("Nothing to deliver: the week is already delivered, or --no-deliver was set.")


@app.command("show")
def show_command(week: WeekOption = None, config: ConfigOption = DEFAULT_SETTINGS_PATH) -> None:
    """Show a week's pulse, stage table, token usage and cost, Doc section and draft."""
    settings = _load(config)
    week = _week(week)
    out = settings.paths.runs / week
    manifest = load_manifest(settings.paths.runs, week)
    if manifest is None:
        raise _fail(f"No run for {week} yet; run `pulse run --week {week}`.", EXIT_PRECONDITION)
    pulse_txt = out / "pulse.txt"
    if pulse_txt.exists():
        typer.echo(pulse_txt.read_text(encoding="utf-8").rstrip())
        typer.echo("")
    else:
        typer.echo("(no pulse composed yet)\n")

    typer.echo(
        f"{'stage':<10} {'status':<8} {'secs':>6} {'calls':>5} {'in tok':>9} "
        f"{'out tok':>8} {'cost $':>7}  notes"
    )
    total_cost = 0.0
    total_in = total_out = 0
    for name in pipeline.STAGES + tuple(s for s in manifest.stages if s not in pipeline.STAGES):
        stage = manifest.stages.get(name)
        if stage is None:
            continue
        secs = (
            f"{(stage.finished_at - stage.started_at).total_seconds():.1f}"
            if stage.started_at and stage.finished_at
            else "-"
        )
        tokens_in = sum(
            stage.usage.get(k, 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        tokens_out = stage.usage.get("output_tokens", 0)
        cost = float(stage.counts.get("cost_usd", 0.0))
        total_cost += cost
        total_in += tokens_in
        total_out += tokens_out
        notes = ", ".join(
            [*(f"{k}={v}" for k, v in stage.counts.items() if k in _SHOW_COUNTS), *stage.flags]
        )
        if stage.error:
            notes = f"{notes}; error: {stage.error}" if notes else f"error: {stage.error}"
        typer.echo(
            f"{name:<10} {stage.status:<8} {secs:>6} {stage.usage.get('calls', 0):>5} "
            f"{tokens_in:>9,} {tokens_out:>8,} {cost:>7.3f}  {notes}"
        )
    typer.echo(
        f"{'total':<10} {'':<8} {'':>6} {'':>5} {total_in:>9,} {total_out:>8,} {total_cost:>7.3f}"
    )

    conn = db.connect(settings.paths.db)
    try:
        run = db.get_run(conn, week)
    finally:
        conn.close()
    delivery = manifest.delivery
    doc_url = (run.doc_url if run else None) or delivery.doc_url
    if doc_url:
        where = ""
        if run and run.doc_section_end_index is not None:
            where = (
                f" (section at indexes {run.doc_section_start_index}–{run.doc_section_end_index})"
            )
        typer.echo(f"\nDoc: {doc_url}{where}")
        section = delivery.section
        if section is not None and not section.complete:
            typer.echo(f"  partly delivered: {len(section.parts)} of 8 parts; run `pulse deliver`")
    else:
        typer.echo("\nDoc: not delivered yet")
    draft_id = (run.draft_id if run else None) or delivery.draft_id
    typer.echo(f"Draft: {draft_id}" if draft_id else "Draft: not created yet")


_SHOW_COUNTS = frozenset(
    {
        "in_window",
        "new",
        "privacy_hits",
        "ok",
        "unclassified",
        "window_classified",
        "window_other_share",
        "reviews",
        "attempts",
        "word_count",
        "from_model",
        "from_fallback",
        "doc_parts_written",
        "drafts_created",
        "reviews_read",
        "themes",
    }
)


# --- eval (T4.6) -----------------------------------------------------------------------------

eval_app = typer.Typer(
    help="Check classification quality against hand labels.", no_args_is_help=True
)
app.add_typer(eval_app, name="eval")
LabelFileOption = Annotated[Path, typer.Option("--file", help="Hand-labelled JSONL file.")]


@eval_app.command("export")
def eval_export(
    week: WeekOption = None,
    file: LabelFileOption = LABEL_FILE,
    config: ConfigOption = DEFAULT_SETTINGS_PATH,
) -> None:
    """Write 100 sample reviews to label by hand (never overwrites existing labels)."""
    settings = _load(config)
    week = _week(week)
    conn = db.connect(settings.paths.db)
    try:
        taxonomy = _active(conn)
        n = export_sheet(conn, settings, week, file)
    except EvalError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc
    finally:
        conn.close()
    ids = ", ".join(sorted(taxonomy.theme_ids())) + ", other"
    typer.echo(f'Wrote {n} reviews to {file}. Set each row\'s "label" to one of: {ids}.')


@eval_app.command("classify")
def eval_classify(
    file: LabelFileOption = LABEL_FILE, config: ConfigOption = DEFAULT_SETTINGS_PATH
) -> None:
    """Classify the hand-labelled reviews and report agreement (target ≥80%)."""
    settings = _load(config)
    conn = db.connect(settings.paths.db)
    try:
        taxonomy = _active(conn)
        report = evaluate(conn, settings, _llm(settings), taxonomy, file)
    except EvalError as exc:
        raise _fail(str(exc), EXIT_PRECONDITION) from exc
    finally:
        conn.close()
    typer.echo(f"Agreement: {report.agreement:.0%} ({report.agree}/{report.total}); target 80%")
    for theme_id, (n, correct) in sorted(report.per_theme.items()):
        typer.echo(f"  {theme_id:<28} {correct}/{n}")
    misses = [(k, v) for k, v in report.confusion.most_common() if k[0] != k[1]]
    if misses:
        typer.echo("Most common disagreements (hand label -> Claude):")
        for (gold, predicted), n in misses[:10]:
            typer.echo(f"  {gold} -> {predicted}: {n}")
