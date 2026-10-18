"""Phases 4–6 end to end, offline: discover → approve → classify → compose with the fake LLM."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pulse.cli import app
from pulse.store import db
from tests import synthetic
from tests.conftest import REPO_ROOT, SETTINGS_PATH

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "llm"


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PULSE_LLM", "fake")
    monkeypatch.setenv("PULSE_LLM_FIXTURES", str(FIXTURES))
    data = yaml.safe_load(SETTINGS_PATH.read_text())
    data["paths"] = {
        "db": str(tmp_path / "pulse.db"),
        "runs": str(tmp_path / "runs"),
        "prompts": str(REPO_ROOT / "prompts"),
        "themes": str(tmp_path / "themes.yaml"),
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    conn = db.connect(tmp_path / "pulse.db")
    db.upsert_reviews(conn, synthetic.reviews())
    conn.close()
    return path


def invoke(*args: str) -> str:
    result = CliRunner().invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_full_offline_pulse(config: Path, tmp_path: Path) -> None:
    week = synthetic.WEEK
    c = str(config)

    out = invoke("themes", "discover", "--week", week, "--config", c)
    assert "Proposed taxonomy v1" in out and "withdrawals_payments" in out

    blocked = CliRunner().invoke(app, ["compose", "--week", week, "--config", c])
    assert blocked.exit_code == 4 and "pulse themes approve" in blocked.output

    invoke("themes", "approve", "v1", "--config", c)
    assert "* active" in invoke("themes", "list", "--config", c)

    out = invoke("classify", "--week", week, "--config", c)
    assert "Classified" in out
    # The coverage sample was already classified for v1 during discovery; the rest now are.
    assert "Classified 0 reviews" in invoke("classify", "--week", week, "--config", c)

    out = invoke("compose", "--week", week, "--config", c)
    runs = tmp_path / "runs" / week
    text = (runs / "pulse.txt").read_text()
    assert text.startswith("Groww Review Pulse — Week of 05 Oct 2026\n")
    assert "1. Withdrawals & Payments" in text
    assert "guardrails passed" in out
    validation = json.loads((runs / "validation.json").read_text())
    assert validation["passed"] and validation["word_count"] <= 250
    stats = json.loads((runs / "stats.json").read_text())
    assert stats["top_theme_ids"] == [
        "withdrawals_payments",
        "app_performance",
        "charges_brokerage",
    ]
    manifest = json.loads((runs / "manifest.json").read_text())
    for stage in ("discover", "classify", "aggregate", "quotes", "compose", "validate"):
        assert manifest["stages"][stage]["status"] == "done", stage
    assert manifest["stages"]["quotes"]["counts"]["from_model"] == 3
    pulse = json.loads((runs / "pulse.json").read_text())
    conn = db.connect(tmp_path / "pulse.db")
    texts = db.review_texts(conn, [q["review_id"] for q in pulse["quotes"]])
    conn.close()
    for q in pulse["quotes"]:
        assert q["quote"] in texts[q["review_id"]]


def test_eval_export_and_classify(config: Path, tmp_path: Path) -> None:
    c = str(config)
    invoke("themes", "discover", "--week", synthetic.WEEK, "--config", c)
    invoke("themes", "approve", "v1", "--config", c)
    sheet = tmp_path / "labels.jsonl"
    out = invoke("eval", "export", "--week", synthetic.WEEK, "--file", str(sheet), "--config", c)
    assert "Wrote 100 reviews" in out
    again = CliRunner().invoke(
        app, ["eval", "export", "--week", synthetic.WEEK, "--file", str(sheet), "--config", c]
    )
    assert again.exit_code == 4 and "not overwriting" in again.output

    # Label every row the way the synthetic data was built, then flip one to force a miss.
    by_text = {t: theme for theme, _, t in synthetic.TEMPLATES}
    rows = [json.loads(line) for line in sheet.read_text().splitlines()]
    for row in rows:
        base = row["text"].rsplit(" (", 1)[0]
        row["label"] = by_text[base]
    rows[0]["label"] = "kyc_onboarding" if rows[0]["label"] != "kyc_onboarding" else "other"
    sheet.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    out = invoke("eval", "classify", "--file", str(sheet), "--config", c)
    assert "Agreement: 99% (99/100)" in out


def test_llm_commands_need_credentials(
    config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PULSE_LLM", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["themes", "discover", "--config", str(config)])
    assert result.exit_code == 4 and "ANTHROPIC_API_KEY" in result.output
