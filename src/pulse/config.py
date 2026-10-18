"""Settings loader (T1.1): `config/settings.yaml` validated into Pydantic models (arch §8).

`${VAR}` in any string value is replaced from the environment. A value that is exactly `${VAR}`
becomes null when the variable is unset, so optional secrets (like the recipient) can be absent
until the command that needs them runs.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

DEFAULT_SETTINGS_PATH = Path("config/settings.yaml")

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PLACEHOLDER = re.compile(r"^\s*(<.*>)?\s*$")


class ConfigError(ValueError):
    """Settings are missing or invalid. The message names the offending key."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlayStoreApp(_Model):
    package_id: str
    lang: str = "en"
    country: str = "in"


class AppConfig(_Model):
    name: str
    play_store: PlayStoreApp


class PlayStoreSourceConfig(_Model):
    enabled: bool = True
    page_size: int = Field(200, ge=1, le=4500)
    page_delay_seconds: float = Field(1.5, ge=0)
    max_reviews: int = Field(30000, ge=1)


class CsvSourceConfig(_Model):
    enabled: bool = False
    path: Path | None = None
    store: str = "csv"
    timezone: str = "Asia/Kolkata"
    date_format: str | None = None
    columns: dict[str, str] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        return _check_timezone(value)

    @model_validator(mode="after")
    def _path_when_enabled(self) -> CsvSourceConfig:
        if self.enabled and self.path is None:
            raise ValueError("path is required when the CSV source is enabled")
        return self


class ReviewFilters(_Model):
    """Which reviews are kept at ingest. Reviews that fail are never stored."""

    min_words: int = Field(9, ge=0)
    drop_emoji: bool = True
    english_only: bool = True


class SourcesConfig(_Model):
    play_store: PlayStoreSourceConfig = PlayStoreSourceConfig()
    csv: CsvSourceConfig = CsvSourceConfig()
    filters: ReviewFilters = ReviewFilters()


class WindowConfig(_Model):
    weeks: int = Field(12, ge=8, le=12)
    timezone: str = "Asia/Kolkata"
    ranking_scope: Literal["window", "latest_week"] = "window"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        return _check_timezone(value)


class AnalysisConfig(_Model):
    max_themes: int = Field(5, ge=1, le=5)
    top_themes: Literal[3] = 3
    min_words_to_classify: int = Field(4, ge=0)
    other_share_alert: float = Field(0.25, ge=0, le=1)
    quote_languages: list[str] = Field(default_factory=lambda: ["en"])
    min_trend_count: int = Field(10, ge=0)  # no trend marker below this many latest-week reviews
    discovery_max_reviews: int = Field(3000, ge=1)  # above this, discovery reads a sample
    discovery_sample_size: int = Field(800, ge=1)
    coverage_sample_size: int = Field(200, ge=1)
    coverage_min_per_rating: int = Field(15, ge=0)
    sample_seed: int = 7
    quote_candidates: int = Field(15, ge=1)
    quote_candidate_max_words: int = Field(60, ge=1)


class PulseConfig(_Model):
    max_words: int = Field(250, ge=1, le=250)
    quote_max_words: int = Field(30, ge=1)
    takeaway_max_words: int = Field(12, ge=1)
    action_max_words: int = Field(20, ge=1)
    composer_samples_per_theme: int = Field(10, ge=0)
    recipient: str | None = None


class LLMStage(_Model):
    model: str
    effort: Effort
    max_tokens: int = Field(16000, ge=1)


class ClassifyStage(LLMStage):
    use_batches: bool = False  # batching deferred (T3.3)
    batch_threshold: int = Field(200, ge=1)
    chunk_size: int = Field(50, ge=1)


class LLMConfig(_Model):
    discover: LLMStage
    classify: ClassifyStage
    quotes: LLMStage
    compose: LLMStage


def _no_send_tools(tools: _Model) -> None:
    """The pipeline only ever creates drafts: refuse to even map a tool that sends."""
    for op, name in tools.model_dump().items():
        if "send" in name.lower():
            raise ValueError(f"{op}: refusing to map a send tool ({name!r}); drafts only")


class McpConfig(_Model):
    url: str
    token: str | None = None  # the server's bearer token, from PULSE_MCP_TOKEN
    timeout_seconds: float = Field(60, gt=0)


class DocsTools(_Model):
    info: str
    append_text: str
    append_table: str

    @model_validator(mode="after")
    def _drafts_only(self) -> DocsTools:
        _no_send_tools(self)
        return self


class DocsDelivery(_Model):
    document: str | None = None  # the running pulse Doc (URL or ID), from PULSE_DOC_ID
    title: str
    tools: DocsTools


class GmailTools(_Model):
    create_draft: str

    @model_validator(mode="after")
    def _drafts_only(self) -> GmailTools:
        _no_send_tools(self)
        return self


class GmailDelivery(_Model):
    subject: str
    tools: GmailTools


class DeliveryConfig(_Model):
    mcp: McpConfig
    docs: DocsDelivery
    gmail: GmailDelivery

    def tool_map(self) -> dict[str, str]:
        """Logical operation -> server tool name."""
        return {
            "docs.info": self.docs.tools.info,
            "docs.append_text": self.docs.tools.append_text,
            "docs.append_table": self.docs.tools.append_table,
            "gmail.create_draft": self.gmail.tools.create_draft,
        }


class PathsConfig(_Model):
    db: Path = Path("data/pulse.db")
    runs: Path = Path("runs")
    prompts: Path = Path("prompts")
    themes: Path = Path("config/themes.yaml")


class Settings(_Model):
    app: AppConfig
    sources: SourcesConfig = SourcesConfig()
    window: WindowConfig = WindowConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    pulse: PulseConfig = PulseConfig()
    llm: LLMConfig
    delivery: DeliveryConfig
    paths: PathsConfig = PathsConfig()

    def check_delivery_ready(self, *, dry_run: bool = False) -> None:
        """Fail if something a real delivery needs is unset (not needed for --no-deliver).

        A dry run makes no MCP calls, so it doesn't need the bearer token.
        """
        needed = {
            "PULSE_DOC_ID (delivery.docs.document)": self.delivery.docs.document,
            "PULSE_RECIPIENT (pulse.recipient)": self.pulse.recipient,
        }
        if not dry_run:
            needed["PULSE_MCP_TOKEN (delivery.mcp.token)"] = self.delivery.mcp.token
        unset = [key for key, value in needed.items() if value is None or _PLACEHOLDER.match(value)]
        if unset:
            raise ConfigError(
                "Delivery is not configured: set "
                + ", ".join(unset)
                + " in .env, or run with --no-deliver."
            )


def load_settings(
    path: Path = DEFAULT_SETTINGS_PATH, *, require_delivery: bool = False
) -> Settings:
    if not path.exists():
        raise ConfigError(f"Settings file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    settings = parse_settings(raw, source=str(path))
    if require_delivery:
        settings.check_delivery_ready()
    return settings


def parse_settings(raw: dict[str, Any], *, source: str = "settings") -> Settings:
    try:
        return Settings.model_validate(_interpolate(raw))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"{source}: {problems}") from exc


def _interpolate(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _interpolate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item) for item in value]
    if isinstance(value, str):
        whole = _ENV_REF.fullmatch(value.strip())
        if whole:
            return os.environ.get(whole.group(1)) or None
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    return value


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {value!r}") from exc
    return value
