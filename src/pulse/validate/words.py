"""Word counting for the ≤250-word limit (T6.3).

A word is a whitespace-separated token containing at least one letter or digit, so bullets,
dashes, stars and trend arrows (`•`, `—`, `★`, `▲`) don't count, and "user-friendly" is one word.
The email's `Full pulse: <url>` line isn't part of the note and is excluded.
"""

from __future__ import annotations

from pulse.ingest.normalize import word_count

FULL_PULSE_PREFIX = "Full pulse:"


def note_word_count(text: str) -> int:
    lines = [line for line in text.splitlines() if not line.strip().startswith(FULL_PULSE_PREFIX)]
    return word_count("\n".join(lines))


__all__ = ["FULL_PULSE_PREFIX", "note_word_count", "word_count"]
