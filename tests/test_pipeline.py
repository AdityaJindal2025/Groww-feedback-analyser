"""Phase 8: `pulse run`, resume, lock, preconditions, exit codes and `pulse show` (T8.1–T8.4)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from pulse import pipeline
from pulse.cli import app
from pulse.config import load_settings
from pulse.deliver.mcp_client import DeliveryError
from pulse.llm.fake import FakeLLM
from pulse.manifest import load_manifest
from pulse.store import db
from pulse.timeutil import window_for
from tests import synthetic
from tests.fakes.fake_gmail_docs_server import Failure, FakeGmailDocsServer
from tests.harness import LLM_FIXTURES, CrashingLLM, Offline, offline

WEEK = synthetic.WEEK


def cli(*args: str) -> Result:
    return CliRunner().invoke(app, list(args))


def run(env: Offline, *args: str) -> Result:
    return cli("run", "--week", WEEK, "--config", str(env.config), *args)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Offline:
    return offline(tmp_path, monkeypatch)


def statuses(env: Offline) -> dict[str, str]:
    manifest = load_manifest(env.runs, WEEK)
    assert manifest is not None
    return {name: stage.status for name, stage in manifest.stages.items()}


# --- T8.1 runner ---------------------------------------------------------------------------


def test_run_does_every_stage_then_is_a_no_op(env: Offline) -> None:
    first = run(env)
    assert first.exit_code == 0, first.output
    assert all(status == "done" for status in statuses(env).values())
    assert set(statuses(env)) == set(pipeline.STAGES)
    assert "Doc section: appended" in first.output
    assert "Gmail draft: created" in first.output
    headings = [e.text for e in env.fake.elements if e.style == "HEADING_1"]
    assert headings == ["Groww Review Pulse — Week of 05 Oct 2026"]
    assert len(env.fake.drafts) == 1

    calls_before = len(env.fake.calls)
    second = run(env)
    assert second.exit_code == 0, second.output
    assert re.findall(r"^\s+(\w+)\s+(\w+)$", second.output, re.M) == [
        (stage, "skipped") for stage in pipeline.STAGES
    ]
    assert len(env.fake.calls) == calls_before  # not even a Doc read
    assert len(env.fake.drafts) == 1 and env.fake.sent == []


def test_crash_during_classify_resumes_without_resending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = offline(tmp_path, monkeypatch, **{"llm.classify.chunk_size": 20})
    settings = load_settings(env.config)
    conn = db.connect(env.db_path)
    fake_llm = FakeLLM(LLM_FIXTURES)
    crashing = CrashingLLM(fake_llm, crash_on_classify_call=3)
    options = pipeline.RunOptions(no_deliver=True)

    with pytest.raises(KeyboardInterrupt):
        pipeline.run(pipeline.RunContext(conn, settings, WEEK, options, llm=crashing))
    saved = len(db.classifications_for(conn, "v1"))
    assert saved == 40  # two chunks of 20 were saved before the "kill"
    assert not (env.runs / WEEK / pipeline.LOCK_NAME).exists()
    assert statuses(env)["classify"] != "done"

    sent_first = _classify_ids(fake_llm)
    fake_llm.calls.clear()
    result = pipeline.run(pipeline.RunContext(conn, settings, WEEK, options, llm=fake_llm))
    sent_again = _classify_ids(fake_llm)
    total = len(db.reviews_in_window(conn, *window_for(WEEK, 12)))
    assert result.stages["classify"] == "ran" and result.stages["ingest"] == "skipped"
    assert len(sent_again) == total - saved  # only the reviews without a saved label
    assert len(sent_first) == 40 and sent_first.isdisjoint(sent_again)
    assert len(db.classifications_for(conn, "v1")) == total
    assert statuses(env)["validate"] == "done"
    conn.close()


def _classify_ids(llm: FakeLLM) -> set[str]:
    texts = set()
    for call in llm.calls:
        if call.stage == "classify":
            texts.update(
                re.findall(r'<review id="r\d+" rating="\d">(.*?)</review>', call.user, re.S)
            )
    return texts


def test_from_compose_reruns_the_build_and_later_stages(env: Offline) -> None:
    assert run(env, "--no-deliver").exit_code == 0
    assert "deliver" not in statuses(env)
    result = run(env, "--from", "compose", "--no-deliver")
    assert result.exit_code == 0, result.output
    outcomes = dict(re.findall(r"^\s+(\w+)\s+(\w+)$", result.output, re.M))
    assert outcomes["ingest"] == "skipped" and outcomes["classify"] == "skipped"
    assert all(outcomes[s] == "ran" for s in pipeline.BUILD_STAGES)
    assert env.fake.calls == []


def test_dry_run_writes_payloads_and_leaves_delivery_pending(env: Offline) -> None:
    result = run(env, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Nothing was sent" in result.output
    assert (env.runs / WEEK / "delivery_payloads.json").exists()
    assert env.fake.calls == []
    assert statuses(env).get("deliver") != "done"
    # The next real run delivers without redoing the earlier stages
    real = run(env)
    assert real.exit_code == 0, real.output
    assert "ingest     skipped" in real.output and "deliver    ran" in real.output


def test_force_redelivers(env: Offline) -> None:
    assert run(env).exit_code == 0
    result = run(env, "--force")
    assert result.exit_code == 0, result.output
    assert "Doc section: appended" in result.output
    assert len(env.fake.drafts) == 2


def test_unknown_from_stage_is_a_precondition(env: Offline) -> None:
    result = run(env, "--from", "nope")
    assert result.exit_code == 4 and "choose one of" in result.output


# --- lock ----------------------------------------------------------------------------------


def test_lock_blocks_a_second_run(env: Offline) -> None:
    lock = env.runs / WEEK / pipeline.LOCK_NAME
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getpid()))  # a live process holds it
    result = run(env)
    assert result.exit_code == 4 and "Another `pulse run`" in result.output
    assert lock.exists()


def test_stale_lock_is_taken_over(env: Offline) -> None:
    lock = env.runs / WEEK / pipeline.LOCK_NAME
    lock.parent.mkdir(parents=True)
    lock.write_text("999999999")
    result = run(env, "--no-deliver")
    assert result.exit_code == 0, result.output
    assert not lock.exists()


# --- T8.3 preconditions and exit codes -----------------------------------------------------


def _one_line_fix(result: Result) -> str:
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert result.exit_code == 4, result.output
    assert len(lines) == 1, result.output
    return lines[0]


def test_missing_salt(env: Offline, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSE_ID_SALT")
    assert "PULSE_ID_SALT" in _one_line_fix(run(env))


def test_missing_claude_credentials(
    env: Offline, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("PULSE_LLM", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    assert "ANTHROPIC_API_KEY" in _one_line_fix(run(env))


def test_no_approved_taxonomy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = offline(tmp_path, monkeypatch, approve=False)
    assert "themes" in _one_line_fix(run(env))


@pytest.mark.parametrize("name", ["PULSE_MCP_TOKEN", "PULSE_DOC_ID", "PULSE_RECIPIENT"])
def test_missing_delivery_settings(
    env: Offline, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name)
    assert name in _one_line_fix(run(env))
    assert env.fake.calls == []
    assert not (env.runs / WEEK / "manifest.json").exists()  # nothing ran


def test_no_deliver_needs_no_delivery_settings(
    env: Offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PULSE_MCP_TOKEN")
    assert run(env, "--no-deliver").exit_code == 0


def test_server_down_is_a_precondition(env: Offline, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(delivery: object) -> None:
        raise DeliveryError("MCP server health check failed (HTTP 502)", precondition=True)

    monkeypatch.setattr("pulse.pipeline.check_server", down)
    assert "health check" in _one_line_fix(run(env))
    assert not (env.runs / WEEK / "manifest.json").exists()


def test_missing_draft_tool_is_a_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real server check connects and lists tools before any work."""
    fake = FakeGmailDocsServer(drafts_enabled=False)
    env = offline(tmp_path, monkeypatch, fake=fake)

    def check(delivery: object) -> None:
        import anyio

        from pulse.deliver.mcp_client import McpClient

        async def connect() -> None:
            async with McpClient.from_settings(delivery):  # type: ignore[arg-type]
                pass

        anyio.run(connect)

    monkeypatch.setattr("pulse.pipeline.check_server", check)
    assert "ENABLE_DRAFTS=true" in _one_line_fix(run(env))
    assert fake.calls == []


def test_delivery_failure_exits_3_and_deliver_retries(env: Offline) -> None:
    env.fake.fail_at(4, Failure("is_error", "Google Docs rejected the request: bad request."))
    failed = run(env)
    assert failed.exit_code == 3, failed.output
    assert "pulse deliver --week" in failed.output
    assert statuses(env)["validate"] == "done" and statuses(env)["deliver"] == "failed"

    retry = cli("deliver", "--week", WEEK, "--config", str(env.config))
    assert retry.exit_code == 0, retry.output
    assert "Doc section: resumed" in retry.output
    headings = [e.text for e in env.fake.elements if e.style == "HEADING_1"]
    assert len(headings) == 1 and len(env.fake.drafts) == 1


def test_healthz_url() -> None:
    assert (
        pipeline.healthz_url("https://x.up.railway.app/mcp") == "https://x.up.railway.app/healthz"
    )
    assert pipeline.healthz_url("https://x.example/mcp/") == "https://x.example/healthz"


# --- T8.4 show -----------------------------------------------------------------------------


def test_show_after_a_run(env: Offline) -> None:
    assert run(env).exit_code == 0
    result = cli("show", "--week", WEEK, "--config", str(env.config))
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.startswith("Groww Review Pulse — Week of 05 Oct 2026")
    for stage in pipeline.STAGES:
        assert re.search(rf"^{stage}\s+done", out, re.M), stage
    assert re.search(r"^total\s+", out, re.M)
    assert f"Doc: {env.fake.url} (section at indexes" in out
    assert "Draft: r-draft-1" in out


def test_show_without_a_run(env: Offline) -> None:
    result = cli("show", "--week", WEEK, "--config", str(env.config))
    assert result.exit_code == 4 and "pulse run" in result.output


# --- T8.2 help -----------------------------------------------------------------------------


COMMANDS = [
    ["ingest"],
    ["themes", "discover"],
    ["themes", "approve"],
    ["themes", "list"],
    ["classify"],
    ["compose"],
    ["deliver"],
    ["run"],
    ["show"],
    ["eval", "export"],
    ["eval", "classify"],
]


@pytest.mark.parametrize("command", COMMANDS, ids=" ".join)
def test_every_command_has_help(command: list[str]) -> None:
    result = cli(*command, "--help")
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    body = result.output.split("Usage:", 1)[1]
    assert len(body.strip().splitlines()) > 2


def test_run_help_lists_its_flags() -> None:
    out = cli("run", "--help").output
    for flag in ("--week", "--dry-run", "--no-deliver", "--from", "--force"):
        assert flag in out


def test_manifest_is_valid_json_after_run(env: Offline) -> None:
    assert run(env).exit_code == 0
    json.loads((env.runs / WEEK / "manifest.json").read_text())
