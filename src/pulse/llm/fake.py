"""Offline stand-in for Claude (T3.6), selected with `PULSE_LLM=fake`.

Responses come from `<fixtures>/<stage>.json` (default `tests/fixtures/llm`, override with
`PULSE_LLM_FIXTURES`). A fixture holds either a fixed `response`, or, for classification,
`label_rules` that label every `<review id=…>` in the request, so the fake works for any chunk,
or, for quotes, `pick_first_candidate`, which quotes each theme's first candidate verbatim.
Fixtures are synthetic; never copy text from `pulse.db` into them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from pulse.llm.client import CallResult, Usage

T = TypeVar("T", bound=BaseModel)

DEFAULT_FIXTURES = Path("tests/fixtures/llm")
_REVIEW = re.compile(r'<review id="([a-z]\d+)" rating="(\d)">(.*?)</review>', re.DOTALL)


@dataclass(frozen=True)
class FakeCall:
    stage: str
    system: str
    user: str


@dataclass
class FakeLLM:
    fixtures_dir: Path = DEFAULT_FIXTURES
    calls: list[FakeCall] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> FakeLLM:
        return cls(Path(os.environ.get("PULSE_LLM_FIXTURES", str(DEFAULT_FIXTURES))))

    def call_structured(self, stage: str, system: str, user: str, schema: type[T]) -> CallResult[T]:
        self.calls.append(FakeCall(stage, system, user))
        path = self.fixtures_dir / f"{stage}.json"
        if not path.exists():
            raise FileNotFoundError(f"no fake LLM fixture for stage {stage!r}: {path}")
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if "label_rules" in fixture:
            data = _label(fixture["label_rules"], user)
        elif fixture.get("pick_first_candidate"):
            data = _first_candidates(user, int(fixture.get("max_words", 30)))
        else:
            data = fixture["response"]
        return CallResult(schema.model_validate(data), Usage(model="fake", calls=1))


def _label(rules: dict[str, Any], user: str) -> dict[str, Any]:
    """Label each review by the first theme whose keywords appear in it; sentiment from rating."""
    items = []
    for rid, rating, text in _REVIEW.findall(user):
        lowered = text.lower()
        theme = next(
            (
                rule["theme_id"]
                for rule in rules["themes"]
                if any(keyword in lowered for keyword in rule["keywords"])
            ),
            rules.get("default_theme", "other"),
        )
        stars = int(rating)
        sentiment = "negative" if stars <= 2 else "neutral" if stars == 3 else "positive"
        person = any(marker in lowered for marker in rules.get("person_markers", []))
        items.append(
            {"rid": rid, "theme_id": theme, "sentiment": sentiment, "mentions_person": person}
        )
    return {"items": items}


_THEME = re.compile(r'<theme id="([^"]+)"[^>]*>(.*?)</theme>', re.DOTALL)


def _first_candidates(user: str, max_words: int) -> dict[str, Any]:
    """Pick candidate c1 of each theme and quote its opening words exactly."""
    picks = []
    for theme_id, body in _THEME.findall(user):
        reviews = _REVIEW.findall(body)
        if reviews:
            cid, _, text = reviews[0]
            picks.append(
                {"theme_id": theme_id, "cid": cid, "quote": " ".join(text.split()[:max_words])}
            )
    return {"picks": picks}
