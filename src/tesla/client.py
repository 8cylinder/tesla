"""aiohttp client construction, credentials and token freshness."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from dotenv import dotenv_values
from tesla_fleet_api import TeslaFleetOAuth
from tesla_fleet_api.const import Scope
from tesla_fleet_api.exceptions import TeslaFleetError

from .config import Config, ConfigError, config_dir, key_path, load_config
from .tokens import Tokens, save_tokens

DEFAULT_REDIRECT_URI = "http://localhost:8085/callback"
HOSTED_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"
TIMEOUT_SECONDS = 30
# partner_accounts/public_key requires vehicle_device_data on the partner
# token; openid alone is enough for register but returns 403 for the lookup.
PARTNER_SCOPES = [Scope.OPENID, Scope.VEHICLE_DEVICE_DATA]
SCOPES = [
    Scope.OPENID,
    Scope.OFFLINE_ACCESS,
    Scope.USER_DATA,
    Scope.VEHICLE_DEVICE_DATA,
    Scope.VEHICLE_CMDS,
    Scope.VEHICLE_CHARGING_CMDS,
    Scope.ENERGY_DEVICE_DATA,
]


class AuthError(Exception):
    """Raised when credentials or tokens need user action."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI


def _dotenv_candidates() -> list[Path]:
    candidates: list[Path] = []
    for path in (Path.cwd() / ".env", config_dir() / ".env"):
        if path not in candidates:
            candidates.append(path)
    return candidates


def _dotenv_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in _dotenv_candidates():
        if not path.is_file():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None and key not in values:
                values[key] = value
    return values


def _first(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def load_credentials(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Credentials:
    """Resolve credentials: explicit arg > environment > .env > error.

    Redirect URI falls back to the registered localhost callback. Secrets are
    only read here, never logged.
    """
    env = os.environ if environ is None else environ
    dotenv = _dotenv_values()
    resolved_id = _first(
        client_id, env.get("TESLA_CLIENT_ID"), dotenv.get("TESLA_CLIENT_ID")
    )
    if resolved_id is None:
        raise AuthError(
            "TESLA_CLIENT_ID is not set; add it to .env or the environment "
            "(see .env.example)"
        )
    resolved_secret = _first(
        client_secret, env.get("TESLA_CLIENT_SECRET"), dotenv.get("TESLA_CLIENT_SECRET")
    )
    if resolved_secret is None:
        raise AuthError(
            "TESLA_CLIENT_SECRET is not set; add it to .env or the environment "
            "(see .env.example)"
        )
    resolved_redirect = _first(
        redirect_uri,
        env.get("TESLA_REDIRECT_URI"),
        dotenv.get("TESLA_REDIRECT_URI"),
        DEFAULT_REDIRECT_URI,
    )
    return Credentials(resolved_id, resolved_secret, resolved_redirect)


def build_oauth(
    session: aiohttp.ClientSession,
    credentials: Credentials,
    config: Config,
    stored: Tokens | None = None,
) -> TeslaFleetOAuth:
    """Create a TeslaFleetOAuth bound to stored tokens when available."""
    return TeslaFleetOAuth(
        session,
        region=config.region,
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
        redirect_uri=credentials.redirect_uri,
        refresh_token=stored.refresh_token if stored else None,
        expires=stored.expires if stored else 0,
    )


@asynccontextmanager
async def oauth_client(
    *,
    credentials: Credentials,
    config: Config | None = None,
    stored: Tokens | None = None,
) -> AsyncIterator[TeslaFleetOAuth]:
    """Yield an OAuth client with a session that closes on exit."""
    config = config or load_config()
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        yield build_oauth(session, credentials, config, stored)


async def build_vehicle(oauth: TeslaFleetOAuth, config: Config):
    """Create the vehicle bound to the configured command transport."""
    vin = config.vehicle.vin
    if vin is None:
        raise ConfigError("no VIN set; run 'tesla config set vehicle.vin <vin>'")
    if config.vehicle.command_mode == "signed":
        await oauth.get_private_key(str(key_path(config)))
        return oauth.vehicles.createSigned(vin)
    return oauth.vehicles.createFleet(vin)


async def ensure_fresh_token(
    oauth: TeslaFleetOAuth, path: Path | None = None
) -> Tokens | None:
    """Refresh when needed and persist Tesla's rotated refresh token.

    Returns the newly stored tokens, or None when the in-memory access token
    was still valid. The library's refresh path raises ValueError rather than
    TeslaFleetError, so both are translated to AuthError here.
    """
    try:
        rotated = await oauth.check_access_token()
    except ValueError as exc:
        raise AuthError(
            f"token refresh failed: {exc}; re-run 'tesla auth login'"
        ) from exc
    if not rotated:
        return None
    tokens = Tokens(refresh_token=rotated["refresh_token"], expires=rotated["expires"])
    save_tokens(tokens, path)
    return tokens


async def exchange_code(
    oauth: TeslaFleetOAuth, code: str, path: Path | None = None
) -> Tokens:
    """Exchange an authorization code for a refresh token, then persist it.

    get_refresh_token() returns None and silently ignores a non-OK response,
    so the result is validated rather than trusted.
    """
    try:
        await oauth.get_refresh_token(code)
    except ValueError as exc:
        raise AuthError(f"authorization failed: {exc}") from exc
    except TeslaFleetError as exc:
        raise AuthError(f"authorization failed: {exc}") from exc
    if not oauth.refresh_token:
        raise AuthError(
            "Tesla did not return a refresh token; the code may be expired "
            "or already used --- re-run 'tesla auth login'"
        )
    tokens = Tokens(refresh_token=oauth.refresh_token, expires=oauth.expires)
    save_tokens(tokens, path)
    return tokens


def parse_callback(raw: str, expected_state: str | None = "login") -> str:
    """Extract the authorization code from a pasted callback URL or bare code."""
    text = raw.strip()
    if not text:
        raise AuthError("no authorization code provided")
    if not text.lower().startswith("http"):
        return text
    query = parse_qs(urlparse(text).query)
    state = query.get("state", [None])[0]
    if expected_state is not None and state != expected_state:
        raise AuthError(
            f"unexpected OAuth state {state!r}; expected {expected_state!r}"
        )
    code = query.get("code", [None])[0]
    if not code:
        raise AuthError("callback URL did not contain a code")
    return code


async def _public_key(config: Config) -> tuple[Path, str]:
    """Load or create the EC P-256 key and return (path, public PEM)."""
    path = key_path(config)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = TeslaFleetOAuth(session, region=config.region, client_id="")
        await client.get_private_key(str(path))
        return path, client.public_pem


async def generate_key(config: Config) -> tuple[Path, str]:
    """Create the private key when absent; return its path and public PEM."""
    path = key_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    return await _public_key(config)


async def load_public_key(config: Config) -> tuple[Path, str]:
    """Load an existing private key and return its path and public PEM."""
    path = key_path(config)
    if not path.exists():
        raise AuthError(f"no private key at {path}; run 'tesla key generate'")
    return await _public_key(config)


async def _partner_token(oauth: TeslaFleetOAuth, credentials: Credentials) -> None:
    """Authenticate as the partner app (client credentials grant).

    partner_login() sets the access token but not expires, so set expires to
    keep the next call from treating the partner token as expired and trying
    a user-token refresh (which would clobber it).
    """
    token = await oauth.partner_login(
        credentials.client_id, credentials.client_secret, PARTNER_SCOPES
    )
    oauth.expires = int(time.time()) + int(token.get("expires_in", 3600))


async def register_domain(
    oauth: TeslaFleetOAuth, credentials: Credentials, domain: str
) -> dict:
    """Register the partner domain with Tesla using a partner token."""
    await _partner_token(oauth, credentials)
    return await oauth.partner.register(domain)


def extract_public_key(data: dict) -> str | None:
    """Pull the PEM out of a partner_accounts/public_key response."""
    response = data.get("response", data)
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("public_key", "key", "pem"):
            value = response.get(key)
            if isinstance(value, str):
                return value
    return None


async def registered_public_key(
    oauth: TeslaFleetOAuth, credentials: Credentials, domain: str
) -> str | None:
    """Return Tesla's registered public key for the domain, if any."""
    await _partner_token(oauth, credentials)
    data = await oauth.partner.public_key(domain)
    return extract_public_key(data)


async def fetch_hosted_key(domain: str) -> str | None:
    """Fetch the public PEM served from the domain's well-known path."""
    url = f"https://{domain}{HOSTED_KEY_PATH}"
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(url) as resp,
    ):
        if resp.status == 404:
            return None
        resp.raise_for_status()
        return await resp.text()


def normalize_pem(pem: str | None) -> str | None:
    """Normalize a PEM for comparison (drop empty lines and spaces)."""
    if pem is None:
        return None
    lines = [line.strip() for line in pem.strip().splitlines()]
    return "\n".join(line for line in lines if line)


def public_point(pem: str) -> str | None:
    """Return the uncompressed EC point hex for a PEM public key."""
    try:
        key = serialization.load_pem_public_key(pem.encode())
    except ValueError:
        return None
    if not isinstance(key, ec.EllipticCurvePublicKey):
        return None
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    ).hex()


def key_point(value: str | None) -> str | None:
    """Normalize a PEM or already-hex public key to an uncompressed point."""
    if value is None:
        return None
    text = value.strip()
    if text.startswith("-----"):
        return public_point(text)
    return text.lower()
