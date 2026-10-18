"""Structured outputs for each LLM stage (T3.2, used by P4–P6).

Count limits live in Pydantic validators, never only in the JSON schema: the schema shapes the
response, the validators decide whether it is acceptable. Checks that need run context (the
expected review IDs, the current top-3 themes) are done by the stage that makes the call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from pulse.models import MAX_THEMES, OTHER_THEME_ID, Action, Takeaway


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- discover (T4.2) -------------------------------------------------------------------------


class ProposedTheme(_Output):
    id: str  # snake_case, e.g. "kyc_onboarding"
    name: str  # ≤ 4 words, e.g. "KYC & Onboarding"
    definition: str  # one sentence
    includes: list[str]  # topics that belong here
    excludes: list[str]  # near-misses that belong elsewhere


class TaxonomyProposal(_Output):
    themes: list[ProposedTheme]

    @model_validator(mode="after")
    def _check(self) -> TaxonomyProposal:
        if not 1 <= len(self.themes) <= MAX_THEMES:
            raise ValueError(f"propose 1–{MAX_THEMES} themes, got {len(self.themes)}")
        ids = [theme.id for theme in self.themes]
        if len(set(ids)) != len(ids):
            raise ValueError("theme ids must be unique")
        if OTHER_THEME_ID in ids:
            raise ValueError(f"don't propose '{OTHER_THEME_ID}'; code adds it")
        return self


# --- classify (T4.4) -------------------------------------------------------------------------


class ReviewLabel(_Output):
    rid: str
    theme_id: str  # a taxonomy id or "other"
    sentiment: Literal["positive", "neutral", "negative"]
    mentions_person: bool


class LabelBatch(_Output):
    items: list[ReviewLabel]

    @model_validator(mode="after")
    def _one_label_per_review(self) -> LabelBatch:
        rids = [item.rid for item in self.items]
        if len(set(rids)) != len(rids):
            raise ValueError("each review id must be labelled exactly once")
        return self


# --- quotes (T5.3) ---------------------------------------------------------------------------


class QuoteChoice(_Output):
    theme_id: str
    cid: str
    quote: str


class QuoteChoices(_Output):
    picks: list[QuoteChoice]

    @model_validator(mode="after")
    def _one_per_theme(self) -> QuoteChoices:
        themes = [pick.theme_id for pick in self.picks]
        if len(set(themes)) != len(themes):
            raise ValueError("pick exactly one quote per theme")
        return self


# --- compose (T6.1) --------------------------------------------------------------------------


class ComposerOutput(_Output):
    takeaways: list[Takeaway]
    actions: list[Action]

    @model_validator(mode="after")
    def _check(self) -> ComposerOutput:
        if len(self.actions) != 3:
            raise ValueError(f"write exactly 3 actions, got {len(self.actions)}")
        themes = [t.theme_id for t in self.takeaways]
        if len(set(themes)) != len(themes):
            raise ValueError("write one takeaway per theme")
        return self


STAGE_OUTPUTS: dict[str, type[BaseModel]] = {
    "discover": TaxonomyProposal,
    "classify": LabelBatch,
    "quotes": QuoteChoices,
    "compose": ComposerOutput,
}
