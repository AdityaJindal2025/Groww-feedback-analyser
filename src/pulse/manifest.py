"""Run manifest files: `runs/<week>/manifest.json` (T1.5).

The manifest records each stage's status, counts, models, prompt versions, token usage,
batch IDs and errors. It never holds review text.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from pulse.models import RunManifest, utc_now

MANIFEST_NAME = "manifest.json"


def manifest_path(runs_dir: Path, week: str) -> Path:
    return runs_dir / week / MANIFEST_NAME


def load_manifest(runs_dir: Path, week: str) -> RunManifest | None:
    path = manifest_path(runs_dir, week)
    if not path.exists():
        return None
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def open_manifest(
    runs_dir: Path, week: str, window_start: datetime, window_end: datetime
) -> RunManifest:
    """Load the week's manifest, or start a new one."""
    existing = load_manifest(runs_dir, week)
    if existing is not None:
        return existing
    return RunManifest(week=week, window_start=window_start, window_end=window_end)


def save_manifest(runs_dir: Path, manifest: RunManifest) -> Path:
    """Write atomically, so a crash never leaves a half-written manifest."""
    manifest.updated_at = utc_now()
    path = manifest_path(runs_dir, manifest.week)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(manifest.model_dump_json(indent=2))
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
