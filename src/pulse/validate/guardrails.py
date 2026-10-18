"""Hard checks before anything is delivered (T6.4, arch §4.7).

Each failure says whether the composer can fix it (takeaways, actions, length). Failures in
stats, taxonomy, quotes or privacy are outside the composer's control and fail the run directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from pulse.analysis.quotes import find_span
from pulse.config import PulseConfig
from pulse.ingest.normalize import word_count
from pulse.models import MAX_THEMES, PulseContent, PulseStats, Taxonomy
from pulse.privacy.scrubber import scan
from pulse.validate.words import note_word_count

TOP_THEMES = 3


class Failure(BaseModel):
    check: str
    message: str
    fixable: bool  # the composer can fix it on a retry


class ValidationReport(BaseModel):
    passed: bool
    word_count: int
    failures: list[Failure]

    @property
    def fixable_only(self) -> bool:
        return bool(self.failures) and all(f.fixable for f in self.failures)


def validate(
    content: PulseContent,
    stats: PulseStats,
    taxonomy: Taxonomy,
    *,
    markdown: str,
    text: str,
    review_texts: Mapping[str, str],
    limits: PulseConfig,
) -> ValidationReport:
    failures: list[Failure] = []

    def fail(check: str, message: str, fixable: bool = False) -> None:
        failures.append(Failure(check=check, message=message, fixable=fixable))

    top = list(stats.top_theme_ids)

    # Structure: 3 themes, 3 quotes, 3 actions, one takeaway per top theme
    if len(top) != TOP_THEMES:
        fail("structure", f"expected {TOP_THEMES} top themes, got {len(top)}")
    if len(content.quotes) != TOP_THEMES:
        fail("structure", f"expected {TOP_THEMES} quotes, got {len(content.quotes)}")
    if len(content.actions) != TOP_THEMES:
        fail("structure", f"write exactly 3 actions (got {len(content.actions)})", fixable=True)
    takeaway_themes = [t.theme_id for t in content.takeaways]
    if sorted(takeaway_themes) != sorted(top):
        fail(
            "structure",
            f"write exactly one takeaway for each top theme ({', '.join(top)}); "
            f"got {', '.join(takeaway_themes) or 'none'}",
            fixable=True,
        )

    # Taxonomy: ≤5 themes, pulse themes drawn from it
    if len(taxonomy.themes) > MAX_THEMES:
        fail("taxonomy", f"taxonomy has {len(taxonomy.themes)} themes (max {MAX_THEMES})")
    unknown = sorted(set(top) - taxonomy.theme_ids())
    if unknown:
        fail("taxonomy", f"top themes not in taxonomy: {', '.join(unknown)}")

    # Verbatim: each quote is a span of its stored review; three different reviews
    for quote in content.quotes:
        source = review_texts.get(quote.review_id)
        if source is None or find_span(quote.quote, source) is None:
            fail("verbatim", f"quote for {quote.theme_id} is not verbatim from its review")
        if word_count(quote.quote) > limits.quote_max_words:
            fail("length", f"quote for {quote.theme_id} exceeds {limits.quote_max_words} words")
    if len({q.review_id for q in content.quotes}) != len(content.quotes):
        fail("verbatim", "quotes must come from different reviews")

    # Grounding: actions and quotes tied to top themes
    for action in content.actions:
        if action.theme_id not in top:
            fail("grounding", f"action tied to {action.theme_id!r}, not a top theme", fixable=True)
    for quote in content.quotes:
        if quote.theme_id not in top:
            fail("grounding", f"quote tied to {quote.theme_id!r}, not a top theme")

    # Length
    words = note_word_count(text)
    if words > limits.max_words:
        fail(
            "length",
            f"note is {words} words (max {limits.max_words}); shorten takeaways and actions",
            fixable=True,
        )
    for takeaway in content.takeaways:
        if word_count(takeaway.text) > limits.takeaway_max_words:
            fail(
                "length",
                f"takeaway for {takeaway.theme_id} exceeds {limits.takeaway_max_words} words",
                fixable=True,
            )
    for action in content.actions:
        if word_count(action.text) > limits.action_max_words:
            fail(
                "length",
                f"action for {action.theme_id} exceeds {limits.action_max_words} words",
                fixable=True,
            )

    # Privacy: nothing the scrubber would redact in either rendering
    for name, body in (("markdown", markdown), ("text", text)):
        hits = scan(body)
        if hits:
            kinds = ", ".join(sorted({h.kind for h in hits}))
            fail("privacy", f"personal-data patterns in the {name} pulse: {kinds}")

    return ValidationReport(passed=not failures, word_count=words, failures=failures)


def write_report(runs_dir: Path, week: str, report: ValidationReport) -> Path:
    path = runs_dir / week / "validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
