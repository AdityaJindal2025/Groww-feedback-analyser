"""Structured calls to Claude (T3.1) with usage and cost tracking (T3.4).

Every call is synchronous (batching is deferred, T3.3) and goes through the beta Messages API so
it can opt into server-side refusal fallbacks (`fallbacks="default"`). The system prompt is sent
as a cached block. The response is validated against a Pydantic model; the model proposes,
code checks.

Opus 5.5 rules this module follows: effort is always set explicitly (its default is `medium`);
no `thinking`, sampling parameters or assistant prefill (all rejected; effort is the control).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, NamedTuple, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from pulse.config import LLMConfig, LLMStage
from pulse.llm.schema import strict_schema
from pulse.models import StageStatus

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute TTL: 1.25 × input


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": Price(1.00, 5.00, 0.10, 1.25),
}


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    calls: int = 0
    fallback: bool = False  # a fallback model served at least one call

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            model=self.model,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            calls=self.calls + other.calls,
            fallback=self.fallback or other.fallback,
        )

    @property
    def cost_usd(self) -> float:
        """Estimate at the configured model's list price (a fallback may bill differently)."""
        price = PRICES.get(self.model)
        if price is None:
            return 0.0
        return (
            self.input_tokens * price.input
            + self.output_tokens * price.output
            + self.cache_read_input_tokens * price.cache_read
            + self.cache_creation_input_tokens * price.cache_write
        ) / 1_000_000


class CallResult(NamedTuple, Generic[T]):
    output: T
    usage: Usage


class LLMError(RuntimeError):
    """A stage's LLM call failed. Messages never contain review text."""


class LLMRefusal(LLMError):
    def __init__(self, stage: str, category: str | None) -> None:
        super().__init__(f"{stage}: Claude declined the request (category: {category or 'none'})")
        self.stage = stage
        self.category = category


class LLMTruncated(LLMError):
    pass


class LLMInvalidOutput(LLMError):
    pass


class LLMUnavailable(LLMError):
    """Rate limit, server or connection error after the SDK's own retries."""


class LLMClient(Protocol):
    def call_structured(self, stage: str, system: str, user: str, schema: type[T]) -> CallResult[T]:
        """Return (validated output, usage) for one stage call."""
        ...


class ClaudeClient:
    def __init__(self, llm: LLMConfig, client: Any | None = None, *, max_retries: int = 3):
        self.llm = llm
        self._client: Any = client or anthropic.Anthropic(max_retries=max_retries)

    def stage_config(self, stage: str) -> LLMStage:
        cfg = getattr(self.llm, stage, None)
        if not isinstance(cfg, LLMStage):
            raise ValueError(f"unknown LLM stage {stage!r}")
        return cfg

    def call_structured(self, stage: str, system: str, user: str, schema: type[T]) -> CallResult[T]:
        cfg = self.stage_config(stage)
        output_format = {"type": "json_schema", "schema": strict_schema(schema)}
        max_tokens = cfg.max_tokens
        content = user
        usage = Usage(model=cfg.model)
        retried_truncation = retried_invalid = False
        while True:
            response = self._create(stage, cfg, system, content, output_format, max_tokens)
            usage = usage + usage_of(response, cfg.model)

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise LLMRefusal(stage, getattr(details, "category", None))
            if response.stop_reason == "max_tokens":
                if retried_truncation:
                    raise LLMTruncated(f"{stage}: output exceeded {max_tokens} tokens twice")
                retried_truncation = True
                max_tokens *= 2
                log.warning("llm output truncated, retrying", extra={"stage": stage})
                continue

            text = final_text(response)
            try:
                return CallResult(schema.model_validate_json(text), usage)
            except ValidationError as exc:
                if retried_invalid:
                    raise LLMInvalidOutput(
                        f"{stage}: output failed validation twice ({exc.error_count()} errors)"
                    ) from exc
                retried_invalid = True
                log.warning(
                    "llm output invalid, retrying",
                    extra={"stage": stage, "errors": exc.error_count()},
                )
                content = user + _validation_feedback(exc)

    def _create(
        self,
        stage: str,
        cfg: LLMStage,
        system: str,
        content: str,
        output_format: dict[str, Any],
        max_tokens: int,
    ) -> Any:
        try:
            return self._client.beta.messages.create(
                model=cfg.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config={"effort": cfg.effort, "format": output_format},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable(f"{stage}: rate limited after retries") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"{stage}: API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"{stage}: could not reach the Claude API") from exc


def final_text(response: Any) -> str:
    """Text of the response, after the last fallback switch (earlier text came from a model
    that then declined). Thinking blocks are skipped."""
    blocks = list(response.content)
    last_fallback = max(
        (i for i, block in enumerate(blocks) if block.type == "fallback"), default=-1
    )
    texts = [block.text for block in blocks[last_fallback + 1 :] if block.type == "text"]
    if not texts:
        raise LLMInvalidOutput("response contained no text block")
    return "".join(texts)


def usage_of(response: Any, model: str) -> Usage:
    u = response.usage
    blocks = getattr(response, "content", [])
    return Usage(
        model=model,
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        calls=1,
        fallback=any(block.type == "fallback" for block in blocks),
    )


def record_usage(stage: StageStatus, usage: Usage) -> None:
    """Add a call's usage to a manifest stage: token counts, model and estimated cost."""
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "calls",
    ):
        stage.usage[key] = stage.usage.get(key, 0) + getattr(usage, key)
    stage.models["llm"] = usage.model
    stage.counts["cost_usd"] = round(float(stage.counts.get("cost_usd", 0.0)) + usage.cost_usd, 4)
    if usage.fallback and "llm_fallback" not in stage.flags:
        stage.flags.append("llm_fallback")


def has_credentials() -> bool:
    """True if the SDK can find Claude credentials: an API key, an auth token, or an
    `ant auth login` profile on disk."""
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return True
    if os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip():
        return True
    profiles = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
    return profiles.is_dir() and any(profiles.iterdir())


def make_client(llm: LLMConfig) -> LLMClient:
    """`PULSE_LLM=fake` selects the offline fake (T3.6); anything else talks to Claude."""
    if os.environ.get("PULSE_LLM", "").strip().lower() == "fake":
        from pulse.llm.fake import FakeLLM

        return FakeLLM.from_env()
    return ClaudeClient(llm)


def _validation_feedback(exc: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in exc.errors(include_input=False)
    )
    return (
        "\n\nYour previous answer did not pass validation: "
        f"{problems}. Answer again, following the schema and rules exactly."
    )


__all__ = [
    "CallResult",
    "ClaudeClient",
    "LLMClient",
    "LLMError",
    "LLMInvalidOutput",
    "LLMRefusal",
    "LLMTruncated",
    "LLMUnavailable",
    "PRICES",
    "Usage",
    "final_text",
    "has_credentials",
    "make_client",
    "record_usage",
]
