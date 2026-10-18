"""JSON-lines logging that never carries review content (T1.5).

Log counts, IDs, stages and timings as `extra` fields. A filter on every handler drops any
record that carries a content field (`text`, `quote`, ...), so review text can't reach logs
even if a caller attaches it by mistake.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import IO, Any

CONTENT_FIELDS = frozenset({"text", "quote", "content", "body"})

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}


class DropContentFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(hasattr(record, name) for name in CONTENT_FIELDS)


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO, stream: IO[str] | None = None) -> logging.Handler:
    """Send `pulse.*` logs as JSON lines to `stream` (stderr by default)."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonLinesFormatter())
    handler.addFilter(DropContentFilter())
    logger = logging.getLogger("pulse")
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return handler
