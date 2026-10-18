from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"

SALT = "test-salt-0123456789-abcdefghijklmnop"


@pytest.fixture
def salt(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("PULSE_ID_SALT", SALT)
    return SALT


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 16, 4, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_pulse_logger() -> Iterator[None]:
    yield
    logger = logging.getLogger("pulse")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.propagate = True
