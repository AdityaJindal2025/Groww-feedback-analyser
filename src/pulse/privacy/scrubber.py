"""Personal-data scrubber (T2.1, arch §4.2).

Runs on every review's text before it is stored or sent to Claude. Patterns are applied in
order: earlier patterns claim text first, so later ones can't mislabel it (an email is never
half-redacted as a UPI handle, a 12-digit Aadhaar never becomes [ID]).

Names inside text can't be caught reliably with regexes; the classifier flags them instead
(`mentions_person`), and such reviews never become quotes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

# (kind, pattern). The replacement token is f"[{kind}]". If a pattern has a named group `id`,
# only that group is replaced and the surrounding words are kept ("ticket id [ID]").
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("URL", re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # Runs after EMAIL, so only dot-less handles such as name@okaxis or 98xxxxxxxx@ybl remain.
    ("UPI", re.compile(r"\b[\w.-]{2,}@[A-Za-z]{2,}\b")),
    ("PAN", re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b")),
    # Not preceded by "+" or a word character, so "+91..." phone numbers aren't read as Aadhaar.
    ("AADHAAR", re.compile(r"(?<![\w+])[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\w)")),
    # Optional +91 / 91 prefix, then a 10-digit mobile number starting 6–9.
    ("PHONE", re.compile(r"(?<![\w+])(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\w)")),
    # Shorter numbers right after an identifier word: "ticket id 28036628", "complaint no: 12345".
    (
        "ID",
        re.compile(
            r"\b(?:ticket|order|ref(?:erence)?|complaint|case|request|txn|transaction|utr"
            r"|account|a/c|acc|client|demat|id)\b[\s.:#-]*(?:id|no\.?|number|num|#)?[\s.:#-]*"
            r"(?P<id>\d{5,8})\b",
            re.IGNORECASE,
        ),
    ),
    # Account, demat, order or ticket numbers.
    ("ID", re.compile(r"\b\d{9,18}\b")),
    # Mixed letters and digits, 8+ characters (order IDs like ORD12345678). May over-redact;
    # that's acceptable because redacted reviews only drop out of quote candidates.
    ("ID", re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{8,}\b")),
)

KINDS: tuple[str, ...] = tuple(dict.fromkeys(kind for kind, _ in PATTERNS))
REDACTION_TOKEN = re.compile(r"\[(?:" + "|".join(KINDS) + r")\]")


@dataclass(frozen=True)
class ScrubResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        return bool(self.counts)


@dataclass(frozen=True)
class Hit:
    """A personal-data match. Holds positions only, never the matched text."""

    kind: str
    start: int
    end: int


def _span(match: re.Match[str]) -> tuple[int, int]:
    return match.span("id") if "id" in match.re.groupindex else match.span()


def _replacer(kind: str) -> Callable[[re.Match[str]], str]:
    def replace(match: re.Match[str]) -> str:
        start, end = _span(match)
        whole = match.group(0)
        offset = match.start()
        return whole[: start - offset] + f"[{kind}]" + whole[end - offset :]

    return replace


def scrub(text: str) -> ScrubResult:
    counts: dict[str, int] = {}
    for kind, pattern in PATTERNS:
        text, n = pattern.subn(_replacer(kind), text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return ScrubResult(text=text, counts=counts)


def scan(text: str) -> list[Hit]:
    """Find anything the scrubber would redact. Empty for clean (already scrubbed) text.

    Like `scrub`, earlier patterns claim text first: a span already matched is not reported
    again under a later pattern.
    """
    hits: list[Hit] = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = _span(match)
            if not any(start < hit.end and hit.start < end for hit in hits):
                hits.append(Hit(kind, start, end))
    return sorted(hits, key=lambda hit: (hit.start, hit.end))


def has_redaction(text: str) -> bool:
    return REDACTION_TOKEN.search(text) is not None
