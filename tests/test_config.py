from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from pulse.config import ConfigError, load_settings, parse_settings
from tests.conftest import SETTINGS_PATH


@pytest.fixture
def raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(SETTINGS_PATH.read_text())
    return copy.deepcopy(data)


def test_repo_settings_load() -> None:
    settings = load_settings(SETTINGS_PATH)
    assert settings.app.play_store.package_id == "com.nextbillion.groww"
    assert settings.window.weeks == 12
    assert settings.llm.classify.chunk_size == 50
    assert settings.delivery.mcp.url.endswith("/mcp")
    assert settings.delivery.tool_map()["gmail.create_draft"] == "gmail_create_draft"


@pytest.mark.parametrize("weeks", [7, 13])
def test_window_weeks_bounds(raw: dict[str, Any], weeks: int) -> None:
    raw["window"]["weeks"] = weeks
    with pytest.raises(ConfigError, match=r"window\.weeks"):
        parse_settings(raw)


@pytest.mark.parametrize("value", [0, 6])
def test_max_themes_bounds(raw: dict[str, Any], value: int) -> None:
    raw["analysis"]["max_themes"] = value
    with pytest.raises(ConfigError, match=r"analysis\.max_themes"):
        parse_settings(raw)


def test_top_themes_must_be_three(raw: dict[str, Any]) -> None:
    raw["analysis"]["top_themes"] = 4
    with pytest.raises(ConfigError, match=r"analysis\.top_themes"):
        parse_settings(raw)


def test_max_words_at_most_250(raw: dict[str, Any]) -> None:
    raw["pulse"]["max_words"] = 300
    with pytest.raises(ConfigError, match=r"pulse\.max_words"):
        parse_settings(raw)


@pytest.mark.parametrize("stage", ["discover", "classify", "quotes", "compose"])
def test_effort_values(raw: dict[str, Any], stage: str) -> None:
    raw["llm"][stage]["effort"] = "extreme"
    with pytest.raises(ConfigError, match=rf"llm\.{stage}\.effort"):
        parse_settings(raw)


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_all_supported_efforts_accepted(raw: dict[str, Any], effort: str) -> None:
    raw["llm"]["compose"]["effort"] = effort
    assert parse_settings(raw).llm.compose.effort == effort


def test_unknown_key_is_rejected(raw: dict[str, Any]) -> None:
    raw["window"]["weekz"] = 12
    with pytest.raises(ConfigError, match=r"window\.weekz"):
        parse_settings(raw)


def test_bad_timezone(raw: dict[str, Any]) -> None:
    raw["window"]["timezone"] = "Mars/Olympus"
    with pytest.raises(ConfigError, match=r"window\.timezone"):
        parse_settings(raw)


def test_csv_enabled_needs_path(raw: dict[str, Any]) -> None:
    raw["sources"]["csv"]["enabled"] = True
    with pytest.raises(ConfigError, match=r"sources\.csv"):
        parse_settings(raw)


def test_recipient_from_environment(raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_RECIPIENT", "pulse-alias@example.com")
    assert parse_settings(raw).pulse.recipient == "pulse-alias@example.com"


def test_recipient_unset_is_none(raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSE_RECIPIENT", raising=False)
    assert parse_settings(raw).pulse.recipient is None


DELIVERY_ENV = ("PULSE_MCP_TOKEN", "PULSE_DOC_ID", "PULSE_RECIPIENT")


def test_unset_delivery_secrets_block_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DELIVERY_ENV:
        monkeypatch.delenv(name, raising=False)
    settings = load_settings(SETTINGS_PATH)
    with pytest.raises(ConfigError, match="PULSE_MCP_TOKEN") as err:
        settings.check_delivery_ready()
    assert "PULSE_DOC_ID" in str(err.value) and "PULSE_RECIPIENT" in str(err.value)
    with pytest.raises(ConfigError, match="--no-deliver"):
        load_settings(SETTINGS_PATH, require_delivery=True)


def test_dry_run_does_not_need_the_token(
    raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PULSE_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PULSE_DOC_ID", "1AbCdoc")
    monkeypatch.setenv("PULSE_RECIPIENT", "pulse-alias@example.com")
    settings = parse_settings(raw)
    settings.check_delivery_ready(dry_run=True)
    with pytest.raises(ConfigError, match="PULSE_MCP_TOKEN"):
        settings.check_delivery_ready()


def test_set_delivery_secrets_pass(raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DELIVERY_ENV:
        monkeypatch.setenv(name, "value@example.com")
    parse_settings(raw).check_delivery_ready()


@pytest.mark.parametrize("section", ["docs", "gmail"])
def test_send_tools_are_rejected(raw: dict[str, Any], section: str) -> None:
    tool = next(iter(raw["delivery"][section]["tools"]))
    raw["delivery"][section]["tools"][tool] = "gmail_send_email"
    with pytest.raises(ConfigError, match="send tool"):
        parse_settings(raw)


def test_missing_file(tmp_path: Any) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")
