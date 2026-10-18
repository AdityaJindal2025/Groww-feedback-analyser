from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pulse.ingest.csv_import import CsvImportError, CsvSource
from pulse.timeutil import IST

SINCE = datetime(2026, 7, 20, tzinfo=IST)
MAPPING = {"text": "review", "rating": "stars", "review_date": "date", "store_review_id": "id"}


def write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text("\n".join(["id,review,stars,date,user_name", *rows]) + "\n", encoding="utf-8")
    return path


def test_valid_mapping_reads_only_mapped_columns(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "export.csv",
        [
            'r1,"Withdrawal stuck, please fix",1,2026-10-06 10:00,Fake Person',
            "r2,Nice app,5,2026-10-07T08:00:00+00:00,Fake Person",
            "r3,Too old,4,2026-06-01 10:00,Fake Person",
        ],
    )
    reviews = list(CsvSource(path, MAPPING).fetch(SINCE))
    assert [r.store_review_id for r in reviews] == ["r1", "r2"]
    assert reviews[0].text == "Withdrawal stuck, please fix"
    assert reviews[0].review_date == datetime(2026, 10, 6, 10, 0, tzinfo=IST)
    assert reviews[1].review_date == datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    assert all(r.store == "csv" for r in reviews)
    assert "Fake Person" not in "".join(r.model_dump_json() for r in reviews)


@pytest.mark.parametrize(
    "column", ["user_name", "Author", "reviewerName", "email", "device_model", "PHONE_NO"]
)
def test_refuses_identity_columns(tmp_path: Path, column: str) -> None:
    with pytest.raises(CsvImportError, match="identity"):
        CsvSource(tmp_path / "x.csv", {**MAPPING, "store_review_id": column})


def test_requires_core_fields(tmp_path: Path) -> None:
    with pytest.raises(CsvImportError, match="rating"):
        CsvSource(tmp_path / "x.csv", {"text": "review", "review_date": "date"})


def test_rejects_unknown_fields(tmp_path: Path) -> None:
    with pytest.raises(CsvImportError, match="unknown"):
        CsvSource(tmp_path / "x.csv", {**MAPPING, "user": "id"})


def test_missing_column_in_file(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "export.csv", ["r1,ok,5,2026-10-06,x"])
    with pytest.raises(CsvImportError, match="thumbs"):
        list(CsvSource(path, {**MAPPING, "thumbs_up": "thumbs"}).fetch(SINCE))


def test_bad_rows_are_skipped(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "export.csv",
        [
            "r1,ok,five,2026-10-06,x",
            "r2,ok,9,2026-10-06,x",
            "r3,ok,4,not-a-date,x",
            "r4,ok,4,2026-10-06,x",
        ],
    )
    source = CsvSource(path, MAPPING)
    assert [r.store_review_id for r in source.fetch(SINCE)] == ["r4"]
    assert source.skipped == 3


def test_ids_derived_when_absent(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "export.csv", ["r1,Same text,3,2026-10-06,x"])
    mapping = {k: v for k, v in MAPPING.items() if k != "store_review_id"}
    first = list(CsvSource(path, mapping).fetch(SINCE))
    second = list(CsvSource(path, mapping).fetch(SINCE))
    assert first[0].store_review_id == second[0].store_review_id


def test_json_and_jsonl(tmp_path: Path) -> None:
    rows = [{"id": "a", "review": "Good", "stars": 5, "date": "2026-10-06T10:00:00+05:30"}]
    (tmp_path / "e.json").write_text(json.dumps(rows))
    (tmp_path / "e.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    for name in ("e.json", "e.jsonl"):
        (review,) = CsvSource(tmp_path / name, MAPPING).fetch(SINCE)
        assert review.rating == 5


def test_custom_date_format(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "export.csv", ["r1,ok,4,06/10/2026,x"])
    (review,) = CsvSource(path, MAPPING, date_format="%d/%m/%Y").fetch(SINCE)
    assert review.review_date == datetime(2026, 10, 6, tzinfo=IST)
