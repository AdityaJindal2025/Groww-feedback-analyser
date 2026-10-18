import pytest

from pulse import env


def test_require_names_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSE_RECIPIENT", raising=False)
    with pytest.raises(env.MissingEnvError, match="PULSE_RECIPIENT"):
        env.require("PULSE_RECIPIENT")


def test_blank_value_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_RECIPIENT", "   ")
    with pytest.raises(env.MissingEnvError, match="PULSE_RECIPIENT"):
        env.require("PULSE_RECIPIENT")


def test_require_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_RECIPIENT", "me@example.com")
    assert env.require("PULSE_RECIPIENT") == "me@example.com"


def test_id_salt_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSE_ID_SALT", raising=False)
    with pytest.raises(env.MissingEnvError, match="PULSE_ID_SALT"):
        env.id_salt()


def test_id_salt_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_ID_SALT", "short")
    with pytest.raises(env.MissingEnvError, match="at least 32"):
        env.id_salt()


def test_id_salt_ok(salt: str) -> None:
    assert env.id_salt() == salt


def test_env_file_is_gitignored() -> None:
    from tests.conftest import REPO_ROOT

    ignored = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored
