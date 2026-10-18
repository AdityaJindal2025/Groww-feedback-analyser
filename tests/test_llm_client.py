"""Structured-call wrapper (T3.1) and usage tracking (T3.4) against a mocked SDK client."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from pulse.config import load_settings
from pulse.llm.client import (
    FALLBACK_BETA,
    ClaudeClient,
    LLMInvalidOutput,
    LLMRefusal,
    LLMTruncated,
    LLMUnavailable,
    Usage,
    has_credentials,
    make_client,
    record_usage,
)
from pulse.llm.fake import FakeLLM
from pulse.llm.outputs import ComposerOutput, LabelBatch, TaxonomyProposal
from pulse.models import StageStatus
from tests.conftest import SETTINGS_PATH

LABELS = {
    "items": [
        {"rid": "r1", "theme_id": "other", "sentiment": "negative", "mentions_person": False},
        {"rid": "r2", "theme_id": "other", "sentiment": "positive", "mentions_person": False},
    ]
}


def block(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def response(
    *blocks: SimpleNamespace,
    stop_reason: str = "end_turn",
    category: str | None = None,
    usage: dict[str, int] | None = None,
) -> SimpleNamespace:
    counts = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    counts.update(usage or {})
    details = SimpleNamespace(type="refusal", category=category) if category else None
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        stop_details=details,
        usage=SimpleNamespace(**counts),
    )


def text(data: Any) -> SimpleNamespace:
    return block("text", text=data if isinstance(data, str) else json.dumps(data))


class MockSDK:
    """Stands in for `anthropic.Anthropic`: returns queued responses, records every request."""

    def __init__(self, *responses: Any) -> None:
        self.queue = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def llm_config() -> Any:
    return load_settings(SETTINGS_PATH).llm


def client_with(llm_config: Any, *responses: Any) -> tuple[ClaudeClient, MockSDK]:
    sdk = MockSDK(*responses)
    return ClaudeClient(llm_config, client=sdk), sdk


def test_success_and_request_shape(llm_config: Any) -> None:
    client, sdk = client_with(llm_config, response(block("thinking", thinking=""), text(LABELS)))

    result = client.call_structured("classify", "SYSTEM", "USER", LabelBatch)

    assert isinstance(result.output, LabelBatch)
    assert [item.rid for item in result.output.items] == ["r1", "r2"]
    (request,) = sdk.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["max_tokens"] == 4000
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert request["betas"] == [FALLBACK_BETA]
    assert request["fallbacks"] == "default"
    assert request["system"] == [
        {"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}
    ]
    assert request["messages"] == [{"role": "user", "content": "USER"}]
    for forbidden in ("thinking", "temperature", "top_p", "top_k"):
        assert forbidden not in request
    assert result.usage.calls == 1 and result.usage.input_tokens == 100


@pytest.mark.parametrize(("stage", "effort"), [("discover", "high"), ("compose", "high")])
def test_effort_and_max_tokens_per_stage(llm_config: Any, stage: str, effort: str) -> None:
    data = {"takeaways": [], "actions": [{"theme_id": "a", "text": "x"}] * 3}
    payload = data if stage == "compose" else {"themes": [_theme("a")]}
    schema = ComposerOutput if stage == "compose" else TaxonomyProposal
    client, sdk = client_with(llm_config, response(text(payload)))
    client.call_structured(stage, "S", "U", schema)
    assert sdk.requests[0]["output_config"]["effort"] == effort
    assert sdk.requests[0]["max_tokens"] == 16000


def test_refusal_raises_with_category(llm_config: Any) -> None:
    client, _ = client_with(llm_config, response(stop_reason="refusal", category="cyber"))
    with pytest.raises(LLMRefusal, match="cyber") as info:
        client.call_structured("classify", "S", "U", LabelBatch)
    assert info.value.category == "cyber"


def test_truncation_retries_once_with_double_budget(llm_config: Any) -> None:
    client, sdk = client_with(
        llm_config, response(text('{"items": ['), stop_reason="max_tokens"), response(text(LABELS))
    )
    result = client.call_structured("classify", "S", "U", LabelBatch)
    assert len(result.output.items) == 2
    assert [r["max_tokens"] for r in sdk.requests] == [4000, 8000]
    assert result.usage.calls == 2


def test_truncation_twice_raises(llm_config: Any) -> None:
    truncated = response(text("{"), stop_reason="max_tokens")
    client, _ = client_with(llm_config, truncated, truncated)
    with pytest.raises(LLMTruncated):
        client.call_structured("classify", "S", "U", LabelBatch)


def test_invalid_json_retries_with_feedback(llm_config: Any) -> None:
    duplicate = {"items": [LABELS["items"][0], LABELS["items"][0]]}
    client, sdk = client_with(llm_config, response(text(duplicate)), response(text(LABELS)))

    result = client.call_structured("classify", "S", "USER TURN", LabelBatch)

    assert len(result.output.items) == 2
    retry = sdk.requests[1]["messages"][0]["content"]
    assert retry.startswith("USER TURN")
    assert "did not pass validation" in retry and "exactly once" in retry


def test_invalid_twice_raises_without_leaking_content(llm_config: Any) -> None:
    client, _ = client_with(llm_config, response(text("not json")), response(text("still not")))
    with pytest.raises(LLMInvalidOutput) as info:
        client.call_structured("classify", "S", "U", LabelBatch)
    assert "still not" not in str(info.value)


def test_fallback_response_uses_text_after_switch(llm_config: Any) -> None:
    fallback = block(
        "fallback",
        from_=SimpleNamespace(model="claude-opus-5-5"),
        to=SimpleNamespace(model="claude-opus-5"),
    )
    client, _ = client_with(llm_config, response(text('{"items": [{"rid"'), fallback, text(LABELS)))
    result = client.call_structured("classify", "S", "U", LabelBatch)
    assert len(result.output.items) == 2
    assert result.usage.fallback


def test_no_text_block_is_invalid(llm_config: Any) -> None:
    client, _ = client_with(llm_config, response(block("thinking", thinking="")))
    with pytest.raises(LLMInvalidOutput, match="no text"):
        client.call_structured("classify", "S", "U", LabelBatch)


def test_connection_error_becomes_unavailable(llm_config: Any) -> None:
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.test"))
    client, _ = client_with(llm_config, error)
    with pytest.raises(LLMUnavailable, match="could not reach"):
        client.call_structured("classify", "S", "U", LabelBatch)


def test_unknown_stage(llm_config: Any) -> None:
    client, _ = client_with(llm_config)
    with pytest.raises(ValueError, match="unknown LLM stage"):
        client.call_structured("summarise", "S", "U", LabelBatch)


# --- usage and cost (T3.4) -----------------------------------------------------------------


def test_cost_at_opus_prices() -> None:
    usage = Usage(
        model="claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=100_000,
    )
    assert usage.cost_usd == pytest.approx(4.00 + 2.00 + 0.20 + 0.50)
    assert Usage(model="unknown-model", input_tokens=10).cost_usd == 0.0


def test_cache_read_is_tracked(llm_config: Any) -> None:
    client, _ = client_with(
        llm_config,
        response(text(LABELS), usage={"cache_creation_input_tokens": 900}),
        response(text(LABELS), usage={"cache_read_input_tokens": 900}),
    )
    first = client.call_structured("classify", "S", "U1", LabelBatch).usage
    second = client.call_structured("classify", "S", "U2", LabelBatch).usage
    total = first + second
    assert total.cache_creation_input_tokens == 900
    assert total.cache_read_input_tokens == 900
    assert total.calls == 2


def test_record_usage_into_manifest_stage() -> None:
    stage = StageStatus()
    usage = Usage(model="claude-opus-5-5", input_tokens=1000, output_tokens=500, calls=1)
    record_usage(stage, usage)
    record_usage(stage, Usage(model="claude-opus-5-5", input_tokens=1000, calls=1, fallback=True))
    assert stage.usage["input_tokens"] == 2000
    assert stage.usage["calls"] == 2
    assert stage.models["llm"] == "claude-opus-5-5"
    assert stage.counts["cost_usd"] == pytest.approx(0.018)
    assert stage.flags == ["llm_fallback"]


# --- client selection and credentials ------------------------------------------------------


def test_make_client_fake(monkeypatch: pytest.MonkeyPatch, llm_config: Any) -> None:
    monkeypatch.setenv("PULSE_LLM", "fake")
    assert isinstance(make_client(llm_config), FakeLLM)


def test_make_client_real(monkeypatch: pytest.MonkeyPatch, llm_config: Any) -> None:
    monkeypatch.delenv("PULSE_LLM", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    assert isinstance(make_client(llm_config), ClaudeClient)


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    yield


def test_has_credentials(
    no_credentials: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert not has_credentials()
    profile_dir = tmp_path / "anthropic"
    profile_dir.mkdir()
    (profile_dir / "profiles.json").write_text("{}")
    assert has_credentials()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert has_credentials()


def _theme(theme_id: str) -> dict[str, Any]:
    return {"id": theme_id, "name": "N", "definition": "D", "includes": [], "excludes": []}
