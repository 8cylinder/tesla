import pytest

from tesla.client import (
    AuthError,
    Credentials,
    ensure_fresh_token,
    load_credentials,
    parse_callback,
)
from tesla.tokens import Tokens, load_tokens


def test_credentials_from_env(monkeypatch, tmp_path, config_dir):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESLA_CLIENT_ID", "env-id")
    monkeypatch.setenv("TESLA_CLIENT_SECRET", "env-secret")
    monkeypatch.delenv("TESLA_REDIRECT_URI", raising=False)
    assert load_credentials() == Credentials("env-id", "env-secret")


def test_credentials_env_wins_over_dotenv(monkeypatch, tmp_path, config_dir):
    (tmp_path / ".env").write_text(
        "TESLA_CLIENT_ID=dotenv-id\nTESLA_CLIENT_SECRET=dotenv-secret\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESLA_CLIENT_ID", "env-id")
    monkeypatch.delenv("TESLA_CLIENT_SECRET", raising=False)
    creds = load_credentials()
    assert creds.client_id == "env-id"
    assert creds.client_secret == "dotenv-secret"


def test_credentials_from_dotenv(monkeypatch, tmp_path, config_dir):
    (tmp_path / ".env").write_text(
        "TESLA_CLIENT_ID=dotenv-id\n"
        "TESLA_CLIENT_SECRET=dotenv-secret\n"
        "TESLA_REDIRECT_URI=http://localhost:9999/cb\n"
    )
    monkeypatch.chdir(tmp_path)
    for name in ("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    assert load_credentials() == Credentials(
        "dotenv-id", "dotenv-secret", "http://localhost:9999/cb"
    )


def test_credentials_missing_raises(monkeypatch, tmp_path, config_dir):
    monkeypatch.chdir(tmp_path)
    for name in ("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AuthError, match="TESLA_CLIENT_ID"):
        load_credentials()


def test_explicit_credentials_win(monkeypatch, tmp_path, config_dir):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESLA_CLIENT_ID", "env-id")
    monkeypatch.setenv("TESLA_CLIENT_SECRET", "env-secret")
    creds = load_credentials(client_id="arg-id", client_secret="arg-secret")
    assert creds == Credentials("arg-id", "arg-secret")


def test_parse_callback_url():
    url = "http://localhost:8085/callback?code=na_abc123&state=login"
    assert parse_callback(url) == "na_abc123"


def test_parse_bare_code():
    assert parse_callback("  na_abc123  ") == "na_abc123"


def test_parse_callback_wrong_state():
    url = "http://localhost:8085/callback?code=na_abc123&state=other"
    with pytest.raises(AuthError, match="state"):
        parse_callback(url)


def test_parse_callback_missing_code():
    with pytest.raises(AuthError, match="did not contain a code"):
        parse_callback("http://localhost:8085/callback?state=login")


def test_parse_callback_empty():
    with pytest.raises(AuthError, match="no authorization code"):
        parse_callback("   ")


class FakeOAuth:
    def __init__(self, rotated=None, error=None):
        self.rotated = rotated
        self.error = error
        self.expires = 123

    async def check_access_token(self):
        if self.error is not None:
            raise self.error
        return self.rotated


async def test_ensure_fresh_token_persists_rotation(config_dir):
    rotated = {"refresh_token": "rotated", "expires": 999}
    tokens = await ensure_fresh_token(FakeOAuth(rotated=rotated))
    assert tokens == Tokens("rotated", 999)
    assert load_tokens() == Tokens("rotated", 999)


async def test_ensure_fresh_token_valid_returns_none(config_dir):
    assert await ensure_fresh_token(FakeOAuth()) is None


async def test_ensure_fresh_token_value_error(config_dir):
    with pytest.raises(AuthError, match="tesla auth login"):
        await ensure_fresh_token(FakeOAuth(error=ValueError("invalid_grant")))
