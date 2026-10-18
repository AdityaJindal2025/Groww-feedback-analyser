import pytest

from pulse.ingest.base import hash_review_id
from pulse.ingest.normalize import (
    detect_script,
    has_emoji,
    has_redaction,
    is_hinglish,
    word_count,
)


@pytest.mark.parametrize(
    ("text", "script"),
    [
        ("Great app for mutual funds, very easy to use", "latin"),
        ("App bahut accha hai, lekin withdrawal slow hai", "latin"),
        ("Café-style UI, très bien", "latin"),
        ("बहुत अच्छा ऐप है", "devanagari"),
        ("👍👍👍", "other"),
        ("", "other"),
        ("12345 !!!", "other"),
        ("App अच्छा है but slow", "other"),
        ("ऐप बहुत धीमा है और पैसे नहीं आते, withdrawal", "devanagari"),
        ("非常好的应用", "other"),
    ],
)
def test_detect_script(text: str, script: str) -> None:
    assert detect_script(text) == script


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("good app", 2),
        ("  good   app  ", 2),
        ("👍 👍 good", 1),
        ("👍👍", 0),
        ("Very bad - worst app", 4),
        ("user-friendly app", 2),
        ("• slow", 1),
        ("बहुत अच्छा", 2),
        ("5 star", 2),
        ("", 0),
    ],
)
def test_word_count(text: str, count: int) -> None:
    assert word_count(text) == count


def test_has_redaction_reexported() -> None:
    assert has_redaction("call [PHONE]")


def test_hash_is_stable_and_salted() -> None:
    a = hash_review_id("play_store", "gp:AOqpTOF", salt="s" * 32)
    assert a == hash_review_id("play_store", "gp:AOqpTOF", salt="s" * 32)
    assert a != hash_review_id("play_store", "gp:AOqpTOF", salt="t" * 32)
    assert a != hash_review_id("csv", "gp:AOqpTOF", salt="s" * 32)
    assert len(a) == 64 and "AOqpTOF" not in a


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Great app 👍", True),
        ("Love it ❤️", True),
        ("5 ⭐ app", True),
        ("rated it ★★★★★", True),
        ("thumbs 👍🏽 up", True),
        ("Plain text, no emoji: ₹500 & 10% off!", False),
        ("बहुत अच्छा", False),
    ],
)
def test_has_emoji(text: str, expected: bool) -> None:
    assert has_emoji(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("app bahut slow hai aur paisa withdraw nahi ho raha", True),
        ("brokerage thoda kam hona chaiya baki sab thik hai", True),
        ("US stock me invest karne ka option kab aaega", True),
        ("Isme bond ka monthly payout correct ni show karta hai", True),
        ("Very slow app, it hangs a lot during market hours", False),
        ("The PE ratio shown on par with other apps is wrong", False),
        ("Please add the option to invest in US stocks soon", False),
        ("I am a retail investor and the charges are too high", False),
        ("", False),
    ],
)
def test_is_hinglish(text: str, expected: bool) -> None:
    assert is_hinglish(text) is expected
