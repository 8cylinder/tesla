import stat
import time

import pytest
from tesla_fleet_api.const import Method

from tesla.client import (
    AuthError,
    Credentials,
    build_oauth,
    dotenv_paths,
    ensure_fresh_token,
    extract_public_key,
    generate_key,
    key_point,
    load_credentials,
    load_public_key,
    normalize_pem,
    parse_callback,
    public_point,
    register_domain,
    registered_public_key,
)
from tesla.config import Config, load_config
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
    for name in (
        "TESLA_CLIENT_ID",
        "TESLA_CLIENT_SECRET",
        "TESLA_REDIRECT_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    assert load_credentials() == Credentials(
        "dotenv-id", "dotenv-secret", "http://localhost:9999/cb"
    )


def test_credentials_missing_raises(monkeypatch, tmp_path, config_dir):
    monkeypatch.chdir(tmp_path)
    for name in (
        "TESLA_CLIENT_ID",
        "TESLA_CLIENT_SECRET",
        "TESLA_REDIRECT_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AuthError, match="TESLA_CLIENT_ID"):
        load_credentials()


def test_dotenv_paths_lists_existing_files(monkeypatch, tmp_path, config_dir):
    monkeypatch.chdir(tmp_path)
    cwd_env = tmp_path / ".env"
    cwd_env.write_text("TESLA_CLIENT_ID=cwd\n")
    config_env = config_dir / ".env"
    config_env.write_text("TESLA_CLIENT_ID=config\n")
    assert dotenv_paths() == [cwd_env, config_env]


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


async def test_generate_key_creates_0600(config_dir):
    config = load_config()
    path, public_pem = await generate_key(config)
    assert path == config_dir / "private_key.pem"
    assert public_pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_generate_key_is_idempotent(config_dir):
    config = load_config()
    _, first = await generate_key(config)
    _, second = await load_public_key(config)
    assert normalize_pem(first) == normalize_pem(second)


async def test_load_public_key_missing(config_dir):
    with pytest.raises(AuthError, match="key generate"):
        await load_public_key(load_config())


def test_extract_public_key_shapes():
    pem = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"
    assert extract_public_key({"response": {"public_key": pem}}) == pem
    assert extract_public_key({"response": pem}) == pem
    assert extract_public_key({"response": {"other": 1}}) is None
    assert extract_public_key({}) is None


def test_normalize_pem():
    messy = "  -----BEGIN PUBLIC KEY-----\n\nabc\n-----END PUBLIC KEY-----  \n"
    clean = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"
    assert normalize_pem(messy) == clean
    assert normalize_pem(None) is None


async def test_key_point_pem_and_hex(config_dir):
    _, pem = await generate_key(load_config())
    point = key_point(pem)
    assert point is not None
    assert point.startswith("04")
    assert len(point) == 130
    assert public_point(pem) == point
    assert key_point(point) == point
    assert key_point(None) is None
    assert key_point("not-a-key") == "not-a-key"


class FakePartner:
    def __init__(self):
        self.domain = None
        self.public_key_result = None

    async def register(self, domain):
        self.domain = domain
        return {"response": {"domain": domain}}

    async def public_key(self, domain):
        return self.public_key_result


class FakePartnerOAuth:
    def __init__(self, public_key_result=None):
        self.partner = FakePartner()
        self.partner.public_key_result = public_key_result
        self.expires = 0
        self.logged_in = None

    async def partner_login(self, client_id, client_secret, scopes=None):
        self.logged_in = (client_id, client_secret, scopes)
        return {"access_token": "tok", "expires_in": 3600}


async def test_register_domain_sets_expires(config_dir):
    oauth = FakePartnerOAuth()
    result = await register_domain(
        oauth, Credentials("id", "secret"), "example.com"
    )
    assert oauth.logged_in[0:2] == ("id", "secret")
    assert oauth.logged_in[2] is not None
    assert oauth.partner.domain == "example.com"
    assert oauth.expires > time.time()
    assert result["response"]["domain"] == "example.com"


async def test_registered_public_key(config_dir):
    pem = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"
    oauth = FakePartnerOAuth(
        public_key_result={"response": {"public_key": pem}}
    )
    assert (
        await registered_public_key(
            oauth, Credentials("id", "secret"), "example.com"
        )
        == pem
    )


async def test_bodyless_posts_get_empty_json(monkeypatch, config_dir):
    calls = []

    async def fake_base(self, method, path, params=None, json=None):
        calls.append((method, path, json))
        return {}

    monkeypatch.setattr("tesla.client.TeslaFleetApi._request", fake_base)
    oauth = build_oauth(object(), Credentials("id", "secret"), Config())
    oauth._access_token = "tok"
    oauth.expires = int(time.time()) + 3600
    await oauth._request(Method.POST, "api/1/vehicles/VIN/wake_up")
    await oauth._request(Method.POST, "api/1/vehicles/VIN/command/charge_start")
    await oauth._request(Method.POST, "cmd", json={"percent": 80})
    assert calls[0][2] == {}
    assert calls[1][2] == {}
    assert calls[2][2] == {"percent": 80}
