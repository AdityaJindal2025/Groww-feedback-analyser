from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pulse.logging import configure_logging
from pulse.manifest import load_manifest, manifest_path, open_manifest, save_manifest

T = datetime(2026, 10, 6, tzinfo=UTC)
SECRET_TEXT = "My PAN is on file and the app keeps crashing at 9:15"


def _capture() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(stream=stream)
    return stream


def test_records_with_review_text_are_dropped() -> None:
    stream = _capture()
    log = logging.getLogger("pulse.ingest")
    log.info("review fetched", extra={"text": SECRET_TEXT})
    log.info("quote picked", extra={"quote": SECRET_TEXT})
    log.info("page done", extra={"page": 3, "yielded": 200})

    output = stream.getvalue()
    assert SECRET_TEXT not in output
    lines = [json.loads(line) for line in output.splitlines()]
    assert len(lines) == 1
    assert lines[0]["event"] == "page done"
    assert lines[0]["page"] == 3
    assert lines[0]["level"] == "INFO"


def test_output_is_json_lines() -> None:
    stream = _capture()
    logging.getLogger("pulse").warning("partial ingest", extra={"source": "play_store"})
    entry = json.loads(stream.getvalue())
    assert entry["source"] == "play_store"
    assert entry["logger"] == "pulse"


def test_manifest_round_trip(tmp_path: Path) -> None:
    manifest = open_manifest(tmp_path, "2026-W41", T, T)
    manifest.start("ingest")
    manifest.finish("ingest", counts={"new": 12, "redactions.PHONE": 1})
    manifest.stage("ingest").flags.append("partial_ingest")
    path = save_manifest(tmp_path, manifest)

    assert path == manifest_path(tmp_path, "2026-W41")
    loaded = load_manifest(tmp_path, "2026-W41")
    assert loaded == manifest
    assert open_manifest(tmp_path, "2026-W41", T, T) == manifest
    assert not list(path.parent.glob(".manifest-*"))


def test_missing_manifest(tmp_path: Path) -> None:
    assert load_manifest(tmp_path, "2026-W41") is None
