"""`pulse run`: the weekly pipeline as one resumable command (T8.1, T8.3).

Stages, in order: ingest → classify → aggregate → quotes → compose → validate → deliver.
Each records its status in `runs/<week>/manifest.json`. A re-run skips stages already `done`
for the week, so a crash or a failed delivery resumes where it stopped; `--from <stage>`
resets that stage and the ones after it.

aggregate, quotes, compose and validate run together (`stages.build_pulse`), so a reset of
any of them reruns all four.

Classification saves every chunk as it completes, so after a crash only the reviews without a
saved label are sent again. Delivery resumes at the first unfinished Doc part.

Preconditions are checked before any work, only for the stages that will run. They read only.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Literal

import anyio
import httpx2

from pulse import env, stages
from pulse.analysis import taxonomy as taxonomies
from pulse.config import ConfigError, DeliveryConfig, Settings
from pulse.deliver.mcp_client import DeliveryError, McpClient
from pulse.deliver.service import ClientFactory, DeliveryReport, deliver_week
from pulse.ingest.base import ReviewSource
from pulse.llm.client import LLMClient, has_credentials, make_client
from pulse.manifest import load_manifest, open_manifest, save_manifest
from pulse.models import StageStatus
from pulse.timeutil import window_for

STAGES: tuple[str, ...] = (
    "ingest",
    "classify",
    "aggregate",
    "quotes",
    "compose",
    "validate",
    "deliver",
)
BUILD_STAGES: tuple[str, ...] = ("aggregate", "quotes", "compose", "validate")
LOCK_NAME = ".lock"

Outcome = Literal["ran", "skipped", "dry_run"]


class PreconditionError(RuntimeError):
    """Something the run needs is missing. The message is one line with the fix."""


class RunLocked(PreconditionError):
    pass


class PrivacyScanFailed(RuntimeError):
    """The store still holds text that matches personal-data patterns after ingest."""


@dataclass
class RunOptions:
    dry_run: bool = False
    no_deliver: bool = False
    force: bool = False
    from_stage: str | None = None


@dataclass
class RunContext:
    conn: sqlite3.Connection
    settings: Settings
    week: str
    options: RunOptions = field(default_factory=RunOptions)
    # Injected in tests; built from settings and the environment otherwise.
    llm: LLMClient | None = None
    sources: list[ReviewSource] | None = None
    client_factory: ClientFactory | None = None
    server_check: Callable[[DeliveryConfig], None] | None = None
    today: date | None = None


@dataclass
class RunResult:
    week: str
    stages: dict[str, Outcome] = field(default_factory=dict)
    delivery: DeliveryReport | None = None


# --- lock ----------------------------------------------------------------------------------


class WeekLock:
    """`runs/<week>/.lock`, holding the owner's PID. A lock whose process is gone is stale and
    is taken over."""

    def __init__(self, runs_dir: Path, week: str) -> None:
        self.path = runs_dir / week / LOCK_NAME

    def __enter__(self) -> WeekLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if _alive(self._owner()):
                    raise RunLocked(
                        f"Another `pulse run` for this week is running (pid {self._owner()}); "
                        f"wait for it, or delete {self.path} if it is gone."
                    ) from None
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            return self
        raise RunLocked(f"Could not take the lock {self.path}; delete it and retry.")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owner() == os.getpid():
            self.path.unlink(missing_ok=True)

    def _owner(self) -> int | None:
        try:
            return int(self.path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None


def _alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- planning and preconditions ------------------------------------------------------------


def stage_status(ctx: RunContext) -> dict[str, StageStatus]:
    manifest = load_manifest(ctx.settings.paths.runs, ctx.week)
    return dict(manifest.stages) if manifest else {}


def plan(ctx: RunContext) -> list[str]:
    """The stages this run will execute, in order."""
    options = ctx.options
    if options.from_stage is not None and options.from_stage not in STAGES:
        raise PreconditionError(
            f"Unknown stage {options.from_stage!r}; choose one of: {', '.join(STAGES)}"
        )
    reset = set(STAGES[STAGES.index(options.from_stage) :]) if options.from_stage else set()
    if reset & set(BUILD_STAGES):
        reset |= set(BUILD_STAGES)
    status = stage_status(ctx)
    todo = [
        stage
        for stage in STAGES
        if stage in reset or status.get(stage, StageStatus()).status != "done"
    ]
    if options.force and "deliver" not in todo:
        todo.append("deliver")
    if options.no_deliver and "deliver" in todo:
        todo.remove("deliver")
    return todo


def check_preconditions(ctx: RunContext, todo: list[str]) -> None:
    """Fail fast, before any work, with a one-line fix. Reads only."""
    settings = ctx.settings
    if "ingest" in todo:
        try:
            env.id_salt()
        except env.MissingEnvError as exc:
            raise PreconditionError(str(exc)) from exc
    llm_stages = {"classify", *BUILD_STAGES}
    if (
        llm_stages & set(todo)
        and ctx.llm is None
        and os.environ.get("PULSE_LLM", "").strip().lower() != "fake"
        and not has_credentials()
    ):
        raise PreconditionError(
            "No Claude credentials: set ANTHROPIC_API_KEY in .env or run `ant auth login`."
        )
    if llm_stages & set(todo):
        try:
            taxonomies.require_active(ctx.conn)
        except taxonomies.TaxonomyError as exc:
            raise PreconditionError(str(exc)) from exc
    if "deliver" in todo:
        try:
            settings.check_delivery_ready(dry_run=ctx.options.dry_run)
        except ConfigError as exc:
            raise PreconditionError(str(exc)) from exc
        if not ctx.options.dry_run:
            check = ctx.server_check or check_server
            try:
                check(settings.delivery)
            except DeliveryError as exc:
                raise PreconditionError(str(exc)) from exc


def healthz_url(mcp_url: str) -> str:
    base = mcp_url.rstrip("/")
    return (base.removesuffix("/mcp") or base) + "/healthz"


def check_server(delivery: DeliveryConfig) -> None:
    """`GET /healthz`, then connect and check that every mapped tool is listed."""
    url = healthz_url(delivery.mcp.url)
    try:
        response = httpx2.get(url, timeout=15)
        healthy = response.status_code == 200 and response.json().get("ok") is True
    except (httpx2.HTTPError, ValueError) as exc:
        raise DeliveryError(
            f"MCP server health check failed ({type(exc).__name__}); check the Railway "
            "deployment, then `pulse deliver`",
            precondition=True,
        ) from exc
    if not healthy:
        raise DeliveryError(
            f"MCP server health check failed (HTTP {response.status_code} from {url}); check "
            "the Railway deployment",
            precondition=True,
        )

    async def list_tools() -> None:
        async with McpClient.from_settings(delivery):
            pass

    anyio.run(list_tools)


# --- running -------------------------------------------------------------------------------


def run(ctx: RunContext) -> RunResult:
    """Run the week's remaining stages. Raises on the first failure; progress is kept."""
    runs = ctx.settings.paths.runs
    result = RunResult(ctx.week)
    with WeekLock(runs, ctx.week):
        todo = plan(ctx)
        check_preconditions(ctx, todo)
        _reset(ctx, todo)
        for stage in STAGES:
            if stage not in todo:
                result.stages[stage] = "skipped"
        if "ingest" in todo:
            _ingest(ctx)
            result.stages["ingest"] = "ran"
        if "classify" in todo:
            stages.classify_window(ctx.conn, ctx.settings, _llm(ctx), ctx.week)
            result.stages["classify"] = "ran"
        if set(BUILD_STAGES) & set(todo):
            stages.build_pulse(ctx.conn, ctx.settings, _llm(ctx), ctx.week)
            for stage in BUILD_STAGES:
                result.stages[stage] = "ran"
        if "deliver" in todo:
            result.delivery = deliver_week(
                ctx.conn,
                ctx.settings,
                ctx.week,
                dry_run=ctx.options.dry_run,
                force=ctx.options.force,
                client_factory=ctx.client_factory,
                today=ctx.today,
            )
            result.stages["deliver"] = "dry_run" if ctx.options.dry_run else "ran"
    result.stages = {stage: result.stages[stage] for stage in STAGES if stage in result.stages}
    return result


def _reset(ctx: RunContext, todo: list[str]) -> None:
    """Mark the stages about to run as pending, so a crash never leaves a stale `done`."""
    runs = ctx.settings.paths.runs
    start, end = window_for(ctx.week, ctx.settings.window.weeks)
    manifest = open_manifest(runs, ctx.week, start, end)
    changed = False
    for stage in todo:
        current = manifest.stages.get(stage)
        if current is not None and current.status == "done":
            manifest.stages[stage] = StageStatus()
            changed = True
    if changed:
        save_manifest(runs, manifest)


def _ingest(ctx: RunContext) -> None:
    result = stages.ingest_window(
        ctx.conn, ctx.settings, ctx.week, salt=env.id_salt(), sources=ctx.sources
    )
    if result.privacy_hits:
        raise PrivacyScanFailed(
            f"Privacy scan failed: {result.privacy_hits} stored reviews still match "
            "personal-data patterns"
        )


def _llm(ctx: RunContext) -> LLMClient:
    if ctx.llm is None:
        ctx.llm = make_client(ctx.settings.llm)
    return ctx.llm
