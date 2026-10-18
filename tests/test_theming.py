"""Phase 4: discovery inputs, taxonomy versions, classification. Offline (fake LLM)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from pulse.analysis import taxonomy as taxonomies
from pulse.analysis.classifier import classify_reviews, system_prompt
from pulse.llm.client import CallResult, LLMRefusal, LLMUnavailable, Usage
from pulse.llm.fake import FakeLLM
from pulse.llm.outputs import LabelBatch, ReviewLabel, TaxonomyProposal
from pulse.llm.prompts import load_prompt
from pulse.models import Review, Taxonomy
from pulse.store import db
from tests import synthetic
from tests.conftest import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "llm"
PROMPTS = REPO_ROOT / "prompts"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "pulse.db")
    db.upsert_reviews(connection, synthetic.reviews())
    yield connection
    connection.close()


def proposal() -> TaxonomyProposal:
    result = FakeLLM(FIXTURES).call_structured("discover", "s", "u", TaxonomyProposal)
    return result.output


def make_taxonomy(conn: sqlite3.Connection, tmp_path: Path) -> Taxonomy:
    saved = taxonomies.save_proposal(conn, proposal(), tmp_path / "themes.yaml")
    return taxonomies.approve(conn, saved.version, tmp_path / "themes.yaml")


# --- T4.1 coverage sample ------------------------------------------------------------------


def skewed(n_by_rating: dict[int, int]) -> list[Review]:
    base = synthetic.reviews(per_template=1)[0]
    out = []
    for rating, n in n_by_rating.items():
        for i in range(n):
            out.append(
                base.model_copy(
                    update={
                        "review_id": f"{rating}-{i:04d}",
                        "rating": rating,
                        "review_date": synthetic.START
                        + (synthetic.END - synthetic.START) * (i / n),
                    }
                )
            )
    return out


def test_coverage_sample_matches_real_skew() -> None:
    # The real window's mix: 1★ 638, 2★ 90, 3★ 137, 4★ 145, 5★ 524
    window = skewed({1: 638, 2: 90, 3: 137, 4: 145, 5: 524})
    sample = taxonomies.coverage_sample(window, size=200, min_per_rating=15, seed=7)
    assert len(sample) == 200
    counts = Counter(r.rating for r in sample)
    assert all(counts[r] >= 15 for r in range(1, 6))
    for rating in (1, 3, 4, 5):  # ratings not lifted by the minimum stay near the window mix
        window_share = sum(1 for r in window if r.rating == rating) / len(window)
        assert abs(counts[rating] / 200 - window_share) <= 0.05


def test_coverage_sample_is_reproducible_and_spread_across_weeks() -> None:
    window = skewed({1: 300, 5: 300})
    first = taxonomies.coverage_sample(window, size=100, min_per_rating=15, seed=7)
    assert first == taxonomies.coverage_sample(window, size=100, min_per_rating=15, seed=7)
    assert first != taxonomies.coverage_sample(window, size=100, min_per_rating=15, seed=8)
    from pulse.timeutil import week_of

    assert len({week_of(r.review_date) for r in first}) == 12


def test_small_window_gives_everything() -> None:
    window = skewed({1: 5, 2: 3})
    assert len(taxonomies.coverage_sample(window, size=200, min_per_rating=15, seed=1)) == 8


def test_discovery_reads_whole_window_unless_huge() -> None:
    window = skewed({1: 50, 5: 50})
    assert taxonomies.discovery_reviews(window, max_reviews=3000, sample_size=10, seed=1) == window
    assert len(taxonomies.discovery_reviews(window, max_reviews=60, sample_size=10, seed=1)) == 10


# --- T4.2, T4.3 versions -------------------------------------------------------------------


def test_versions_approval_and_yaml(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "config" / "themes.yaml"
    v1 = taxonomies.save_proposal(conn, proposal(), path)
    v2 = taxonomies.save_proposal(conn, proposal(), path)
    assert (v1.version, v2.version) == ("v1", "v2")
    assert not v1.approved and db.active_taxonomy(conn) is None

    taxonomies.approve(conn, "v1", path)
    taxonomies.approve(conn, "v2", path)
    active = db.active_taxonomy(conn)
    assert active is not None and active.version == "v2"
    data: dict[str, Any] = yaml.safe_load(path.read_text())
    assert [(v["version"], v["approved"]) for v in data["versions"]] == [
        ("v1", False),
        ("v2", True),
    ]
    assert len(data["versions"][0]["themes"]) == 5


def test_require_active_explains_next_step(conn: sqlite3.Connection) -> None:
    with pytest.raises(taxonomies.TaxonomyError, match="pulse themes discover"):
        taxonomies.require_active(conn)


def test_approve_unknown_version(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(taxonomies.TaxonomyError, match="v9"):
        taxonomies.approve(conn, "v9", tmp_path / "t.yaml")


# --- T4.4 prompt determinism ---------------------------------------------------------------


def test_classify_system_prompt_is_deterministic(conn: sqlite3.Connection, tmp_path: Path) -> None:
    taxonomy = make_taxonomy(conn, tmp_path)
    prompt = load_prompt("classify", PROMPTS)
    assert system_prompt(prompt, taxonomy).encode() == system_prompt(prompt, taxonomy).encode()


# --- T4.5 classifier -----------------------------------------------------------------------


class Scripted:
    """An LLM that answers each classify call from a list of behaviours."""

    def __init__(self, *behaviours: Any) -> None:
        self.behaviours = list(behaviours)
        self.calls: list[str] = []

    def call_structured(self, stage: str, system: str, user: str, schema: Any) -> CallResult[Any]:
        self.calls.append(user)
        behaviour = self.behaviours.pop(0)
        if isinstance(behaviour, Exception):
            raise behaviour
        labels = behaviour(user)
        return CallResult(LabelBatch(items=labels), Usage(model="scripted", calls=1))


def label_all(theme: str = "app_performance", skip: int = 0, bad: int = 0) -> Any:
    import re

    def answer(user: str) -> list[ReviewLabel]:
        rids = re.findall(r'id="(r\d+)"', user)
        out = []
        for i, rid in enumerate(rids[skip:]):
            out.append(
                ReviewLabel(
                    rid=rid,
                    theme_id="nonsense" if i < bad else theme,
                    sentiment="negative",
                    mentions_person=False,
                )
            )
        return out

    return answer


def test_classifier_saves_and_second_run_classifies_nothing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    taxonomy = make_taxonomy(conn, tmp_path)
    prompt = load_prompt("classify", PROMPTS)
    todo = db.unclassified_reviews(conn, synthetic.START, synthetic.END, taxonomy.version, 4)
    assert len(todo) == 120

    report, _ = classify_reviews(conn, todo, taxonomy, FakeLLM(FIXTURES), prompt, chunk_size=50)

    assert (report.chunks, report.ok, report.unclassified) == (3, 120, 0)
    assert report.by_theme["withdrawals_payments"] == 40
    assert report.other_share == pytest.approx(10 / 120)
    assert db.unclassified_reviews(conn, synthetic.START, synthetic.END, taxonomy.version, 4) == []
    saved = db.classifications_for(conn, taxonomy.version)
    assert {c.prompt_version for c in saved} == {prompt.version}
    assert {c.model for c in saved} == {"fake"}


def test_missing_and_invalid_labels_get_one_retry(conn: sqlite3.Connection, tmp_path: Path) -> None:
    taxonomy = make_taxonomy(conn, tmp_path)
    chunk = synthetic.reviews()[:10]
    llm = Scripted(label_all(skip=2, bad=1), label_all(skip=1))  # retry still misses one
    report, rows = classify_reviews(
        conn, chunk, taxonomy, llm, load_prompt("classify", PROMPTS), chunk_size=10
    )
    assert len(llm.calls) == 2
    assert llm.calls[1].count("<review ") == 3  # only the 3 bad reviews are retried
    assert (report.ok, report.unclassified, report.retried) == (9, 1, 3)
    assert sum(1 for r in rows if r.status == "unclassified") == 1


def test_refused_chunk_is_retried_then_unclassified(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    taxonomy = make_taxonomy(conn, tmp_path)
    chunk = synthetic.reviews()[:5]
    llm = Scripted(LLMRefusal("classify", "cyber"), LLMRefusal("classify", "cyber"))
    report, _ = classify_reviews(
        conn, chunk, taxonomy, llm, load_prompt("classify", PROMPTS), chunk_size=5
    )
    assert (report.ok, report.unclassified) == (0, 5)


def test_unavailable_stops_run_but_keeps_finished_chunks(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    taxonomy = make_taxonomy(conn, tmp_path)
    todo = synthetic.reviews()[:30]
    llm = Scripted(label_all(), LLMUnavailable("classify: rate limited"))
    with pytest.raises(LLMUnavailable):
        classify_reviews(conn, todo, taxonomy, llm, load_prompt("classify", PROMPTS), chunk_size=10)
    assert len(db.classifications_for(conn, taxonomy.version)) == 10
    left = db.unclassified_reviews(conn, synthetic.START, synthetic.END, taxonomy.version, 4)
    assert len(left) == 110
