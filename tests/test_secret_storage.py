import sys

import pytest

from aigauge.secret_storage import load_secret, save_secret


@pytest.fixture(autouse=True)
def _allow_plaintext_on_non_windows(monkeypatch):
    """Production refuses to write secrets on non-Windows; tests opt in explicitly."""
    if sys.platform != "win32":
        monkeypatch.setenv("AIGAUGE_ALLOW_PLAINTEXT_SECRETS", "1")


def test_round_trip_short():
    save_secret("test-short", "hello")
    assert load_secret("test-short") == "hello"


def test_round_trip_large():
    """The whole point of this module: handle values >2.5KB that keyring can't."""
    big = "x" * 20_000  # 20KB — comfortably exceeds Credential Manager's limit
    save_secret("test-large", big)
    assert load_secret("test-large") == big


def test_overwrite():
    save_secret("test-overwrite", "first")
    save_secret("test-overwrite", "second")
    assert load_secret("test-overwrite") == "second"


def test_delete():
    save_secret("test-delete", "value")
    save_secret("test-delete", None)
    assert load_secret("test-delete") is None


def test_load_missing_returns_none():
    assert load_secret("never-set-this-key") is None


@pytest.mark.skipif(sys.platform == "win32", reason="plaintext gate is non-Windows only")
def test_plaintext_read_refused_without_opt_in(monkeypatch):
    save_secret("gated", "value")
    monkeypatch.delenv("AIGAUGE_ALLOW_PLAINTEXT_SECRETS", raising=False)
    assert load_secret("gated") is None


@pytest.mark.skipif(sys.platform == "win32", reason="plaintext gate is non-Windows only")
def test_plaintext_file_is_owner_only(tmp_path):
    import os

    from aigauge.secret_storage import _secrets_path

    save_secret("perm-check", "value")
    mode = os.stat(_secrets_path()).st_mode & 0o777
    assert mode == 0o600


def test_multiple_secrets_independent():
    save_secret("a", "alpha")
    save_secret("b", "beta")
    assert load_secret("a") == "alpha"
    assert load_secret("b") == "beta"
    save_secret("a", None)
    assert load_secret("a") is None
    assert load_secret("b") == "beta"
