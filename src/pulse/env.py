"""Environment variables and secrets (T0.2).

Secrets live only in the environment (or a gitignored `.env`), never in the repo or config.
Each command asks for the variables it needs, so `pulse ingest` works without a recipient.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
PULSE_RECIPIENT = "PULSE_RECIPIENT"
PULSE_ID_SALT = "PULSE_ID_SALT"
PULSE_MCP_TOKEN = "PULSE_MCP_TOKEN"
PULSE_DOC_ID = "PULSE_DOC_ID"

MIN_SALT_LENGTH = 32


class MissingEnvError(RuntimeError):
    """A required environment variable is missing or invalid."""


def load_env(path: Path | None = None) -> None:
    """Load `.env` from the working directory without overriding variables already set."""
    load_dotenv(path or Path.cwd() / ".env", override=False)


def optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def require(name: str) -> str:
    value = optional(name)
    if value is None:
        raise MissingEnvError(
            f"Missing required environment variable {name}. Set it in .env (see .env.example)."
        )
    return value


def id_salt() -> str:
    """The salt for hashing store review IDs. Changing it breaks de-duplication."""
    salt = require(PULSE_ID_SALT)
    if len(salt) < MIN_SALT_LENGTH:
        raise MissingEnvError(
            f"{PULSE_ID_SALT} must be at least {MIN_SALT_LENGTH} characters "
            f"(it has {len(salt)}). Generate one once and never change it."
        )
    return salt
