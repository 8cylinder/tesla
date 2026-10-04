"""Click command groups for the tesla CLI."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import click
from rich.console import Console
from rich.table import Table
from tesla_fleet_api.exceptions import (
    InvalidScope,
    InvalidToken,
    LoginRequired,
    OAuthExpired,
    TeslaFleetError,
)

from . import __version__
from .client import (
    SCOPES,
    AuthError,
    Credentials,
    ensure_fresh_token,
    exchange_code,
    load_credentials,
    oauth_client,
    parse_callback,
)
from .config import ConfigError, dump_toml, load_config, save_config, set_dotted
from .logging import setup_logging
from .tokens import TokenError, Tokens, load_tokens

console = Console()
err_console = Console(stderr=True)

_AUTH_FAULTS = (OAuthExpired, InvalidToken, LoginRequired)


def _fail(message: str, code: int = 2) -> None:
    err_console.print(f"[red]error:[/red] {message}")
    raise SystemExit(code)


@contextmanager
def _api_errors():
    """Translate known failures into exit codes: 2 auth/config, 3 API."""
    try:
        yield
    except (AuthError, ConfigError, TokenError) as exc:
        _fail(str(exc))
    except InvalidScope as exc:
        _fail(f"missing scope: {exc}; re-run 'tesla auth login' to grant it")
    except _AUTH_FAULTS as exc:
        _fail(f"authentication failed: {exc}; re-run 'tesla auth login'")
    except TeslaFleetError as exc:
        _fail(f"Tesla API error: {exc}", 3)


def _format_expiry(expires: int) -> str:
    return datetime.fromtimestamp(expires).astimezone().isoformat(timespec="seconds")


async def _respond(writer, status: str, message: str) -> None:
    body = f"<html><body><p>{message}</p></body></html>".encode()
    headers = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(headers.encode() + body)
    await writer.drain()


async def _wait_for_callback(redirect_uri: str, timeout: float = 300.0) -> str:
    """Serve the registered callback once and return the authorization code."""
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8085
    expected_path = parsed.path or "/"
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    async def handle(reader, writer) -> None:
        try:
            request_line = await reader.readline()
            parts = request_line.decode("latin-1").split()
            path = parts[1] if len(parts) >= 2 else "/"
            if path.split("?", 1)[0] != expected_path:
                await _respond(writer, "404 Not Found", "Not the callback path.")
                return
            try:
                code = parse_callback(f"http://{host}:{port}{path}")
            except AuthError as exc:
                if not result.done():
                    result.set_exception(exc)
                await _respond(writer, "400 Bad Request", str(exc))
                return
            if not result.done():
                result.set_result(code)
            await _respond(writer, "200 OK", "Login captured. You can close this tab.")
        except (ConnectionError, ValueError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port)
    async with server:
        try:
            return await asyncio.wait_for(result, timeout)
        finally:
            server.close()
            await server.wait_closed()


async def _login(credentials: Credentials, listen: bool) -> Tokens:
    config = load_config()
    async with oauth_client(credentials=credentials, config=config) as oauth:
        url = oauth.get_login_url(SCOPES)
        console.print("Open this URL on any phone or laptop and authorize:")
        console.print(url, highlight=False, soft_wrap=True)
        if listen:
            parsed = urlparse(credentials.redirect_uri)
            host = parsed.hostname or "localhost"
            port = parsed.port or 8085
            console.print(f"Waiting for the callback on http://{host}:{port} ...")
            code = await _wait_for_callback(credentials.redirect_uri)
        else:
            console.print(
                "The browser will fail to load after authorizing --- that is "
                "expected. Copy the full address-bar URL and paste it here."
            )
            raw = click.prompt("Callback URL (or code)")
            code = parse_callback(raw)
        return await exchange_code(oauth, code)


def _render_products(data: dict) -> None:
    items = data.get("response")
    if not isinstance(items, list):
        _fail("unexpected products response", 3)
    vehicles = [item for item in items if item.get("vin")]
    sites = [item for item in items if "energy_site_id" in item]
    if vehicles:
        table = Table(title="Vehicles")
        table.add_column("VIN")
        table.add_column("State")
        table.add_column("ID")
        for item in vehicles:
            table.add_row(
                str(item.get("vin", "")),
                str(item.get("state", "")),
                str(item.get("id", "")),
            )
        console.print(table)
    if sites:
        table = Table(title="Energy sites")
        table.add_column("Site ID")
        table.add_column("Type")
        table.add_column("Name")
        for item in sites:
            table.add_row(
                str(item["energy_site_id"]),
                str(item.get("resource_type", "")),
                str(item.get("site_name", "")),
            )
        console.print(table)
    if not vehicles and not sites:
        console.print("No products found.")


@click.group()
@click.version_option(__version__, prog_name="tesla")
@click.option("-v", "--verbose", count=True, help="Increase log verbosity (-v, -vv).")
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write logs to FILE instead of stderr.",
)
def cli(verbose: int, log_file: Path | None) -> None:
    """Tesla Fleet API CLI: Powerwall-aware vehicle charging."""
    setup_logging(verbose, log_file)


@cli.group()
def auth() -> None:
    """Authenticate against the Tesla Fleet API."""


@auth.command("login")
@click.option(
    "--listen",
    is_flag=True,
    help="Wait for the callback on localhost instead of pasting the URL.",
)
def auth_login(listen: bool) -> None:
    """Run the headless OAuth login and save the refresh token."""
    with _api_errors():
        credentials = load_credentials()
        tokens = asyncio.run(_login(credentials, listen))
    console.print(
        f"[green]Logged in.[/green] Refresh token saved, "
        f"expires {_format_expiry(tokens.expires)}."
    )


@auth.command("status")
def auth_status() -> None:
    """Show token validity, refreshing and persisting rotation if needed."""
    with _api_errors():
        config = load_config()
        stored = load_tokens()
        if stored is None:
            _fail("not logged in; run 'tesla auth login'")

        async def run() -> tuple[int, bool]:
            async with oauth_client(
                credentials=load_credentials(), config=config, stored=stored
            ) as oauth:
                rotated = await ensure_fresh_token(oauth)
                return oauth.expires, rotated is not None

        expires, rotated = asyncio.run(run())
    if rotated:
        console.print("[green]Access token refreshed; rotation persisted.[/green]")
    else:
        console.print("[green]Access token is valid.[/green]")
    console.print(f"Expires {_format_expiry(expires)}")


@cli.command()
def products() -> None:
    """List vehicles and energy sites on the account."""
    with _api_errors():
        config = load_config()
        stored = load_tokens()
        if stored is None:
            _fail("not logged in; run 'tesla auth login'")

        async def run() -> dict:
            async with oauth_client(
                credentials=load_credentials(), config=config, stored=stored
            ) as oauth:
                await ensure_fresh_token(oauth)
                return await oauth.products()

        data = asyncio.run(run())
    _render_products(data)


@cli.group()
def config() -> None:
    """View and update configuration."""


@config.command("show")
def config_show() -> None:
    """Print the effective configuration."""
    try:
        current = load_config()
    except ConfigError as exc:
        _fail(str(exc))
    click.echo(dump_toml(current), nl=False)


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set dotted KEY (e.g. auto_charge.threshold) to VALUE."""
    try:
        current = load_config()
        set_dotted(current, key, value)
        save_config(current)
    except ConfigError as exc:
        _fail(str(exc))
    console.print(f"[green]set[/green] {key} = {value}")


def main() -> None:
    cli()
