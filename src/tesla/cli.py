"""Click command groups for the tesla CLI."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import aiohttp
import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from tesla_fleet_api.exceptions import (
    InvalidScope,
    InvalidToken,
    LibraryError,
    LoginRequired,
    OAuthExpired,
    TeslaFleetError,
    VehicleOffline,
)

from . import __version__
from .auto_charge import (
    AlreadyRunning,
    LockFile,
    TeslaAutoChargeClient,
)
from .auto_charge import run as run_auto_charge
from .car import (
    ChargeOnSolarSettings,
    charge_state,
    is_plugged,
    vehicle_status,
)
from .car import (
    charge_on_solar_settings as read_cos_settings,
)
from .client import (
    HOSTED_KEY_PATH,
    SCOPES,
    AuthError,
    Credentials,
    build_vehicle,
    ensure_fresh_token,
    env_value,
    exchange_code,
    fetch_hosted_key,
    generate_key,
    key_point,
    load_credentials,
    load_public_key,
    oauth_client,
    parse_callback,
    public_point,
    register_domain,
    registered_public_key,
)
from .completions import install_bash_completion
from .config import (
    Config,
    ConfigError,
    config_path,
    dump_toml,
    key_path,
    load_config,
    lock_path,
    notify_state_path,
    save_config,
    set_dotted,
    tokens_path,
)
from .logging import setup_logging
from .notify import NotifyError, NtfyNotifier, build_notifier
from .powerwall import backup_reserve, grid_status, percentage_charged
from .tokens import TokenError, Tokens, load_tokens

LOGGER = logging.getLogger(__name__)

_AUTH_FAULTS = (OAuthExpired, InvalidToken, LoginRequired)


@dataclass
class AppState:
    """Per-invocation runtime state."""

    config: Config | None
    config_error: str | None
    console: Console
    err_console: Console


class _HelpOptions:
    def __init__(  # noqa: ANN401
        self,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        context_settings = dict(kwargs.pop("context_settings", {}) or {})
        context_settings.setdefault("help_option_names", ["-h", "--help"])
        super().__init__(
            *args,
            context_settings=context_settings,  # ty: ignore[unknown-argument]
            **kwargs,
        )


class AppCommand(_HelpOptions, click.Command):
    pass


class AppGroup(_HelpOptions, click.Group):
    command_class = AppCommand
    group_class = type


def _state() -> AppState:
    ctx = click.get_current_context()
    if isinstance(ctx.obj, AppState):
        return ctx.obj
    raise RuntimeError("application state is not initialized")


def _current_console(stderr: bool = False) -> Console:
    ctx = click.get_current_context(silent=True)
    if ctx is not None and isinstance(ctx.obj, AppState):
        return ctx.obj.err_console if stderr else ctx.obj.console
    return Console(stderr=stderr)


class _ConsoleProxy:
    def __init__(self, stderr: bool = False) -> None:
        self._stderr = stderr

    def print(  # noqa: ANN401
        self,
        *objects: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        _current_console(self._stderr).print(*objects, **kwargs)


console = _ConsoleProxy()
err_console = _ConsoleProxy(stderr=True)


def _color_setting(color: str) -> bool | None:
    mode = color.lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("FORCE_COLOR", "") not in ("", "0"):
        return True
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return None


def _build_consoles(color: str) -> tuple[Console, Console]:
    setting = _color_setting(color)
    if setting is True:
        return (
            Console(force_terminal=True),
            Console(stderr=True, force_terminal=True),
        )
    if setting is False:
        return (
            Console(color_system=None, no_color=True),
            Console(stderr=True, color_system=None, no_color=True),
        )
    return Console(), Console(stderr=True)


def _fail(message: str, code: int = 2) -> NoReturn:
    _current_console(stderr=True).print(f"[red]error:[/red] {escape(message)}")
    raise SystemExit(code)


def _warn(message: str) -> None:
    _current_console(stderr=True).print(
        f"[yellow]warning:[/yellow] {escape(message)}"
    )


def _config() -> Config:
    state = _state()
    if state.config is None:
        _fail(state.config_error or "configuration is invalid")
    return state.config


def config_callback(
    ctx: click.Context,
    _param: click.Parameter,
    value: bool,
) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(config_path())
    ctx.exit()


def completions_callback(
    ctx: click.Context,
    _param: click.Parameter,
    value: bool,
) -> None:
    if not value or ctx.resilient_parsing:
        return
    if not isinstance(ctx.command, click.Group):
        raise click.ClickException("--completions requires a command group")
    install_bash_completion(
        ctx.command,
        ctx.info_name or "tesla",
        __version__,
    )
    ctx.exit()


@contextmanager
def _api_errors() -> Generator[None]:
    """Translate known failures into exit codes: 2 auth/config, 3 API."""
    try:
        yield
    except (AuthError, ConfigError, TokenError) as exc:
        _fail(str(exc))
    except InvalidScope as exc:
        _fail(f"missing scope: {exc}; re-run 'tesla auth login' to grant it")
    except _AUTH_FAULTS as exc:
        _fail(f"authentication failed: {exc}; re-run 'tesla auth login'")
    except LibraryError as exc:
        _fail(f"library error: {exc}", 3)
    except aiohttp.ClientError as exc:
        _fail(f"network error: {exc}", 3)
    except TeslaFleetError as exc:
        _fail(f"Tesla API error: {exc}", 3)


def _format_expiry(expires: int) -> str:
    return (
        datetime.fromtimestamp(expires)
        .astimezone()
        .isoformat(timespec="seconds")
    )


async def _respond(
    writer: asyncio.StreamWriter,
    status: str,
    message: str,
) -> None:
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

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            parts = request_line.decode("latin-1").split()
            path = parts[1] if len(parts) >= 2 else "/"
            if path.split("?", 1)[0] != expected_path:
                await _respond(
                    writer, "404 Not Found", "Not the callback path."
                )
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
            await _respond(
                writer, "200 OK", "Login captured. You can close this tab."
            )
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
    config = _config()
    async with oauth_client(credentials=credentials, config=config) as oauth:
        url = oauth.get_login_url(SCOPES)
        console.print("Open this URL on any phone or laptop and authorize:")
        console.print(escape(url), highlight=False, soft_wrap=True)
        if listen:
            parsed = urlparse(credentials.redirect_uri)
            host = parsed.hostname or "localhost"
            port = parsed.port or 8085
            console.print(
                f"Waiting for the callback on http://{escape(host)}:{port} ..."
            )
            code = await _wait_for_callback(credentials.redirect_uri)
        else:
            console.print(
                "The browser will fail to load after authorizing --- that is "
                "expected. Copy the full address-bar URL and paste it here."
            )
            raw = click.prompt("Callback URL (or code)")
            code = parse_callback(raw)
        return await exchange_code(oauth, code)


def _render_products(data: dict[str, Any]) -> None:
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
                escape(str(item.get("vin", ""))),
                escape(str(item.get("state", ""))),
                escape(str(item.get("id", ""))),
            )
        console.print(table)
    if sites:
        table = Table(title="Energy sites")
        table.add_column("Site ID")
        table.add_column("Type")
        table.add_column("Name")
        for item in sites:
            table.add_row(
                escape(str(item["energy_site_id"])),
                escape(str(item.get("resource_type", ""))),
                escape(str(item.get("site_name", ""))),
            )
        console.print(table)
    if not vehicles and not sites:
        console.print("No products found.")


@click.group(cls=AppGroup, no_args_is_help=True)
@click.version_option(package_name="tesla", prog_name="tesla")
@click.option(
    "--config",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=config_callback,
    help="Print the config file path and exit.",
)
@click.option(
    "--completions",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=completions_callback,
    help="Write bash completions and exit.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase log verbosity (-v, -vv).",
)
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write logs to FILE instead of stderr.",
)
@click.option(
    "--color",
    type=click.Choice(["auto", "always", "never"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Control colored output.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: int,
    log_file: Path | None,
    color: str,
) -> None:
    """Tesla Fleet API CLI: Powerwall-aware vehicle charging."""
    out_console, err_console_obj = _build_consoles(color)
    state = AppState(
        config=None,
        config_error=None,
        console=out_console,
        err_console=err_console_obj,
    )
    ctx.obj = state
    ctx.color = _color_setting(color)
    setup_logging(verbose, log_file, console=err_console_obj)
    try:
        state.config = load_config(
            on_unknown=lambda key: _warn(f"ignoring unknown config key {key!r}")
        )
    except ConfigError as exc:
        state.config_error = str(exc)
        if ctx.invoked_subcommand != "doctor":
            _fail(state.config_error)


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
        config = _config()
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
        console.print(
            "[green]Access token refreshed; rotation persisted.[/green]"
        )
    else:
        console.print("[green]Access token is valid.[/green]")
    console.print(f"Expires {_format_expiry(expires)}")


@auth.command("logout")
def auth_logout() -> None:
    """Delete the stored refresh token (it is not revoked with Tesla)."""
    path = tokens_path()
    if not path.exists():
        console.print("No stored token; nothing to do.")
        return
    path.unlink()
    console.print(
        f"[green]Removed[/green] {escape(str(path))}. "
        "Run 'tesla auth login' to authorize again."
    )


@cli.group()
def notify() -> None:
    """Configure and test ntfy notifications."""


@notify.command("test")
@click.option("--message", default="test notification from the tesla CLI")
def notify_test(message: str) -> None:
    """Send a test notification using the configured ntfy topic."""
    with _api_errors():
        config = _config()
        if not config.notify.enabled:
            _fail(
                "notifications are disabled; run "
                "'tesla config set notify.enabled true'"
            )
        try:
            notifier = build_notifier(config)
        except NotifyError as exc:
            _fail(str(exc))
        if notifier is None:  # pragma: no cover - enabled checked above
            _fail("notifications are disabled")
        ok = asyncio.run(
            notifier.send("test", message, title="tesla notify test")
        )
    if not ok:
        _fail("failed to send; check the URL/topic and logs", 3)
    console.print("[green]notification sent[/green]")


@notify.command("reset")
def notify_reset() -> None:
    """Clear notification cooldown state so stages notify again."""
    path = notify_state_path()
    if path.exists():
        path.unlink()
        console.print(f"[green]Cleared[/green] {escape(str(path))}")
    else:
        console.print("No notification state to clear.")


@cli.command()
def products() -> None:
    """List vehicles and energy sites on the account."""
    with _api_errors():
        config = _config()
        stored = load_tokens()
        if stored is None:
            _fail("not logged in; run 'tesla auth login'")

        async def run() -> dict[str, Any]:
            async with oauth_client(
                credentials=load_credentials(), config=config, stored=stored
            ) as oauth:
                await ensure_fresh_token(oauth)
                return await oauth.products()

        data = asyncio.run(run())
    _render_products(data)


def _render_powerwall(
    live: dict[str, Any],
    info: dict[str, Any] | None = None,
) -> None:
    pct = percentage_charged(live)
    reserve = backup_reserve(info or {})
    if reserve is None:
        reserve = backup_reserve(live)
    grid = grid_status(live)
    table = Table(title="Powerwall")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row(
        "Battery", f"{pct:.1f}%" if pct is not None else "unavailable"
    )
    table.add_row(
        "Backup reserve",
        f"{reserve:.0f}%" if reserve is not None else "unavailable",
    )
    table.add_row("Grid status", escape(grid or "unavailable"))
    console.print(table)


@cli.group()
def powerwall() -> None:
    """Powerwall status and notifications."""


_ETA_WINDOW = 5
_IDLE_INTERVAL = 3600.0


def _format_clock_time(timestamp: float) -> str:
    """Render a local time as e.g. ``1:10pm``."""
    moment = datetime.fromtimestamp(timestamp)
    hour = moment.hour % 12 or 12
    meridiem = "am" if moment.hour < 12 else "pm"
    return f"{hour}:{moment.minute:02d}{meridiem}"


def _estimate_eta(
    history: list[tuple[float, float]], level: float
) -> float | None:
    """Least-squares extrapolation to ``level``; None when not rising."""
    if len(history) < 2:
        return None
    last_t, last_pct = history[-1]
    if last_pct >= level:
        return None
    mean_t = sum(t for t, _ in history) / len(history)
    mean_pct = sum(pct for _, pct in history) / len(history)
    variance = sum((t - mean_t) ** 2 for t, _ in history)
    if variance == 0:
        return None
    slope = (
        sum((t - mean_t) * (pct - mean_pct) for t, pct in history) / variance
    )
    if slope <= 0:
        return None
    return last_t + (level - last_pct) / slope


async def _notify_on_level(
    notifier: NtfyNotifier,
    read_pct: Callable[[], Awaitable[float | None]],
    *,
    level: float,
    interval: float,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Poll the level and notify once per crossing of ``level``.

    After the notification, polling slows to once an hour until the level
    dips below again, when the normal interval and ETA history resume.
    """
    sleep = sleep or asyncio.sleep
    clock = clock or time.time
    idle_interval = max(interval, _IDLE_INTERVAL)
    history: list[tuple[float, float]] = []
    notified = False
    poll_interval = interval
    while True:
        pct = await read_pct()
        if pct is None:
            LOGGER.info("Powerwall reading unavailable")
        else:
            if pct >= level:
                history.clear()
                if not notified:
                    LOGGER.info(
                        "Powerwall reached %g%%; sending notification", level
                    )
                    notified = await notifier.send(
                        "powerwall_level",
                        f"Powerwall reached {level:g}%",
                        tags=("battery",),
                    )
                else:
                    LOGGER.info("Powerwall at %.1f%%; already notified", pct)
            else:
                history.append((clock(), pct))
                history = history[-_ETA_WINDOW:]
                eta = _estimate_eta(history, level)
                eta_text = f"; eta: {_format_clock_time(eta)}" if eta else ""
                if notified:
                    LOGGER.info(
                        "Powerwall dropped to %.1f%%; notification re-armed",
                        pct,
                    )
                else:
                    LOGGER.info(
                        "Powerwall at %.1f%%; waiting for %g%%%s",
                        pct,
                        level,
                        eta_text,
                    )
                notified = False
        next_interval = idle_interval if notified else interval
        if next_interval != poll_interval:
            if next_interval > poll_interval:
                LOGGER.info(
                    "poll interval slowed to %.0f s until the next cycle",
                    next_interval,
                )
            else:
                LOGGER.info("poll interval back to %.0f s", next_interval)
            poll_interval = next_interval
        await sleep(poll_interval)


@powerwall.command("status")
@click.option("--watch", is_flag=True, help="Poll until interrupted.")
@click.option(
    "--interval",
    type=click.IntRange(min=5),
    default=30,
    show_default=True,
    help="Seconds between polls with --watch.",
)
def powerwall_status(watch: bool, interval: int) -> None:
    """Show battery percentage, reserve, and grid status."""
    with _api_errors():
        config = _config()
        site_id = config.powerwall.energy_site_id
        if site_id is None:
            _fail(
                "no energy_site_id set; run "
                "'tesla config set powerwall.energy_site_id <id>'"
            )
        credentials = load_credentials()
        stored = load_tokens()
        if stored is None:
            _fail("not logged in; run 'tesla auth login'")

        async def run() -> None:
            async with oauth_client(
                credentials=credentials, config=config, stored=stored
            ) as oauth:
                site = oauth.energySites.create(site_id)
                while True:
                    await ensure_fresh_token(oauth)
                    _render_powerwall(
                        await site.live_status(), await site.site_info()
                    )
                    if not watch:
                        return
                    await asyncio.sleep(interval)

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            console.print("stopped")


@powerwall.command("notify")
@click.option(
    "--level",
    "-l",
    type=click.FloatRange(0.0, 100.0, min_open=True),
    default=100.0,
    show_default=True,
    help="Notify when the battery reaches this percentage.",
)
@click.option(
    "--interval",
    type=click.IntRange(min=5),
    default=300,
    show_default=True,
    help="Seconds between polls.",
)
def powerwall_notify(level: float, interval: int) -> None:
    """Watch the Powerwall and notify once per cycle at LEVEL%."""
    logging.getLogger("tesla.cli").setLevel(logging.INFO)
    with _api_errors():
        config = _config()
        site_id = config.powerwall.energy_site_id
        if site_id is None:
            _fail(
                "no energy_site_id set; run "
                "'tesla config set powerwall.energy_site_id <id>'"
            )
        if not config.notify.enabled:
            _fail(
                "notifications are disabled; run "
                "'tesla config set notify.enabled true'"
            )
        try:
            notifier = build_notifier(config)
        except NotifyError as exc:
            _fail(str(exc))
        if notifier is None:  # pragma: no cover - enabled checked above
            _fail("notifications are disabled")
        credentials = load_credentials()
        stored = load_tokens()
        if stored is None:
            _fail("not logged in; run 'tesla auth login'")

        async def run() -> None:
            async with oauth_client(
                credentials=credentials, config=config, stored=stored
            ) as oauth:
                site = oauth.energySites.create(site_id)

                async def read_pct() -> float | None:
                    await ensure_fresh_token(oauth)
                    return percentage_charged(await site.live_status())

                await _notify_on_level(
                    notifier, read_pct, level=level, interval=interval
                )

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            console.print("stopped")


def _render_car(
    data: dict[str, Any],
    settings: ChargeOnSolarSettings | None = None,
) -> None:
    state = charge_state(data)
    if state is None:
        _fail("vehicle did not return charge state (asleep or offline?)", 3)
    table = Table(title="Vehicle")
    table.add_column("Field")
    table.add_column("Value")
    battery = state.get("battery_level")
    table.add_row(
        "Battery", f"{battery}%" if battery is not None else "unavailable"
    )
    range_value = state.get("battery_range")
    table.add_row(
        "Range",
        f"{range_value} mi" if range_value is not None else "unavailable",
    )
    table.add_row(
        "Charging state",
        escape(str(state.get("charging_state", "unknown"))),
    )
    plugged = is_plugged(data)
    plugged_text = {None: "unknown", True: "yes", False: "no"}[plugged]
    table.add_row("Plugged in", plugged_text)
    limit = state.get("charge_limit_soc")
    table.add_row(
        "Charge limit", f"{limit}%" if limit is not None else "unavailable"
    )
    status = vehicle_status(data)
    if settings is None:
        cos_text = "unknown"
    else:
        cos_text = "enabled" if settings.enabled else "disabled"
    table.add_row("Charge on solar", cos_text)
    if status.cos_state:
        detail = status.cos_state
        if status.cos_reason:
            detail += f" ({status.cos_reason})"
        table.add_row("CoS state", escape(detail))
    if settings is not None and (
        settings.lower_limit is not None or settings.upper_limit is not None
    ):
        lower = (
            "?" if settings.lower_limit is None else f"{settings.lower_limit:g}"
        )
        upper = (
            "?" if settings.upper_limit is None else f"{settings.upper_limit:g}"
        )
        table.add_row("CoS limits", f"{lower}% to {upper}%")
    console.print(table)


async def _read_car_data(  # noqa: ANN401
    vehicle: Any,  # noqa: ANN401
    config: Config,
) -> dict[str, Any]:
    """Read charge state, waking the vehicle once when it is asleep."""
    try:
        return await vehicle.vehicle_data(endpoints=["charge_state"])
    except VehicleOffline:
        LOGGER.info("vehicle asleep; waking car to read status")
        await vehicle.wake_up(
            wait=True, timeout=config.auto_charge.wake_timeout_seconds
        )
        return await vehicle.vehicle_data(endpoints=["charge_state"])


@cli.group()
def car() -> None:
    """Vehicle status and charging commands."""


@car.command("status")
def car_status() -> None:
    """Show battery, charging state, plug status, charge limit, and CoS."""
    logging.getLogger("tesla.cli").setLevel(logging.INFO)
    with _api_errors():
        config = _config()
        vin = config.vehicle.vin
        if not vin:
            _fail("no VIN set; run 'tesla config set vehicle.vin <vin>'")
        credentials = load_credentials()
        stored = load_tokens()
        if stored is None:
            _fail("not logged in; run 'tesla auth login'")

        async def run() -> tuple[dict[str, Any], ChargeOnSolarSettings | None]:
            async with oauth_client(
                credentials=credentials, config=config, stored=stored
            ) as oauth:
                await ensure_fresh_token(oauth)
                vehicle = oauth.vehicles.createFleet(vin)
                data = await _read_car_data(vehicle, config)
                settings = None
                if config.vehicle.command_mode == "signed":
                    await oauth.get_private_key(str(key_path(config)))
                    signed = oauth.vehicles.createSigned(vin)
                    try:
                        settings = read_cos_settings(
                            await signed.get_charge_on_solar()
                        )
                    except TeslaFleetError:
                        settings = None
                return data, settings

        data, settings = asyncio.run(run())
    _render_car(data, settings)


def _car_context() -> tuple[Config, Credentials, Tokens]:
    config = _config()
    if config.vehicle.vin is None:
        _fail("no VIN set; run 'tesla config set vehicle.vin <vin>'")
    credentials = load_credentials()
    stored = load_tokens()
    if stored is None:
        _fail("not logged in; run 'tesla auth login'")
    return config, credentials, stored


async def _send_manual_notification(config: Config, message: str) -> None:
    try:
        notifier = build_notifier(config)
    except NotifyError as exc:
        err_console.print(f"[yellow]notify:[/yellow] {escape(str(exc))}")
        return
    if notifier is not None:
        await notifier.send("manual", message, priority=3, tags=("wrench",))


async def _car_action(  # noqa: ANN401
    config: Config,
    credentials: Credentials,
    stored: Tokens,
    action: Callable[[Any], Awaitable[Any]],
    *,
    notify_message: str,
) -> Any:  # noqa: ANN401
    """Wake the vehicle, run one command on it, and return the response."""
    async with oauth_client(
        credentials=credentials, config=config, stored=stored
    ) as oauth:
        await ensure_fresh_token(oauth)
        vehicle = await build_vehicle(oauth, config)
        await vehicle.wake_up(
            wait=True, timeout=config.auto_charge.wake_timeout_seconds
        )
        result = await action(vehicle)
    await _send_manual_notification(config, notify_message)
    return result


@car.group("charge")
def car_charge() -> None:
    """Send manual charging commands (wakes the vehicle first)."""


@car_charge.command("start")
def car_charge_start() -> None:
    """Start charging now."""
    with _api_errors():
        config, credentials, stored = _car_context()
        result = asyncio.run(
            _car_action(
                config,
                credentials,
                stored,
                lambda v: v.charge_start(),
                notify_message="manual charge_start sent",
            )
        )
    console.print("[green]charge_start sent[/green]")
    if result:
        console.print(escape(str(result)))


@car_charge.command("stop")
def car_charge_stop() -> None:
    """Stop charging now."""
    with _api_errors():
        config, credentials, stored = _car_context()
        result = asyncio.run(
            _car_action(
                config,
                credentials,
                stored,
                lambda v: v.charge_stop(),
                notify_message="manual charge_stop sent",
            )
        )
    console.print("[green]charge_stop sent[/green]")
    if result:
        console.print(escape(str(result)))


@car_charge.command("limit")
@click.argument("percent", type=click.IntRange(0, 100))
def car_charge_limit(percent: int) -> None:
    """Set the charge limit to PERCENT (0-100)."""
    with _api_errors():
        config, credentials, stored = _car_context()
        result = asyncio.run(
            _car_action(
                config,
                credentials,
                stored,
                lambda v: v.set_charge_limit(percent),
                notify_message=f"manual charge limit set to {percent}%",
            )
        )
    console.print(f"[green]set_charge_limit {percent}% sent[/green]")
    if result:
        console.print(escape(str(result)))


@cli.command()
def doctor() -> None:
    """Check config, credentials, auth, connectivity, IDs, and keys."""
    rows: list[tuple[str, bool, str]] = []
    config: Config | None = None
    credentials: Credentials | None = None
    stored: Tokens | None = None

    state = _state()
    if state.config is not None:
        config = state.config
        rows.append(("config", True, f"region={config.region}"))
    else:
        rows.append(
            ("config", False, state.config_error or "invalid configuration")
        )

    try:
        credentials = load_credentials()
        rows.append(("credentials", True, "client id and secret found"))
    except AuthError as exc:
        rows.append(("credentials", False, str(exc)))

    try:
        stored = load_tokens()
        if stored is None:
            rows.append(
                ("tokens", False, "not logged in; run 'tesla auth login'")
            )
        else:
            rows.append(("tokens", True, "refresh token present"))
    except TokenError as exc:
        rows.append(("tokens", False, str(exc)))

    if config is not None:
        vin = config.vehicle.vin
        site_id = config.powerwall.energy_site_id
        rows.append(("vehicle.vin", vin is not None, vin or "not set"))
        rows.append(
            (
                "powerwall.energy_site_id",
                site_id is not None,
                str(site_id) if site_id is not None else "not set",
            )
        )
        if config.vehicle.command_mode == "signed":
            path = key_path(config)
            rows.append(("private key", path.exists(), str(path)))
        if config.notify.enabled:
            topic = env_value("TESLA_NTFY_TOPIC")
            rows.append(
                (
                    "notify",
                    bool(topic),
                    "topic set" if topic else "TESLA_NTFY_TOPIC not set",
                )
            )
        else:
            rows.append(("notify", True, "disabled"))

    api_failed = False
    if config is not None and credentials is not None and stored is not None:

        async def fetch_products() -> dict[str, Any]:
            async with oauth_client(
                credentials=credentials, config=config, stored=stored
            ) as oauth:
                await ensure_fresh_token(oauth)
                return await oauth.products()

        try:
            data = asyncio.run(fetch_products())
        except (
            AuthError,
            LibraryError,
            TeslaFleetError,
            aiohttp.ClientError,
        ) as exc:
            api_failed = True
            rows.append(("api", False, str(exc)))
        else:
            items = [
                i for i in (data.get("response") or []) if isinstance(i, dict)
            ]
            vins = {i.get("vin") for i in items}
            sites = {i.get("energy_site_id") for i in items}
            rows.append(
                ("api", True, f"{len(vins)} vehicles, {len(sites)} sites")
            )
            if config.vehicle.vin:
                rows.append(
                    (
                        "vin listed",
                        config.vehicle.vin in vins,
                        config.vehicle.vin,
                    )
                )
            if config.powerwall.energy_site_id:
                rows.append(
                    (
                        "site id listed",
                        config.powerwall.energy_site_id in sites,
                        str(config.powerwall.energy_site_id),
                    )
                )

    table = Table(title="tesla doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    failed = False
    for name, ok, detail in rows:
        failed = failed or not ok
        table.add_row(
            escape(name),
            "[green]ok[/green]" if ok else "[red]fail[/red]",
            escape(detail),
        )
    console.print(table)
    if failed:
        raise SystemExit(3 if api_failed else 2)


@cli.command("auto-charge")
@click.option(
    "--threshold",
    type=click.FloatRange(0.0, 100.0, min_open=True),
    default=None,
    help="Override auto_charge.threshold.",
)
@click.option(
    "--interval",
    type=click.IntRange(min=5),
    default=None,
    help="Override the poll interval in seconds.",
)
@click.option(
    "--dry-run", is_flag=True, help="Log the decision without sending commands."
)
@click.option("--once", is_flag=True, help="Single evaluation pass, then exit.")
def auto_charge(
    threshold: float | None, interval: int | None, dry_run: bool, once: bool
) -> None:
    """Watch the Powerwall and start charging once at the threshold."""
    logging.getLogger("tesla.auto_charge").setLevel(logging.INFO)
    with _api_errors():
        config = _config()
        if threshold is not None:
            config.auto_charge.threshold = threshold
        if interval is not None:
            config.auto_charge.poll_interval_seconds = interval
        if config.vehicle.vin is None:
            _fail("no VIN set; run 'tesla config set vehicle.vin <vin>'")
        if config.powerwall.energy_site_id is None:
            _fail(
                "no energy_site_id set; run "
                "'tesla config set powerwall.energy_site_id <id>'"
            )
        credentials = load_credentials()
        stored = load_tokens()
        if stored is None:
            _fail("not logged in; run 'tesla auth login'")
        try:
            notifier = build_notifier(config)
        except NotifyError as exc:
            _fail(str(exc))

        lock = LockFile(lock_path())
        try:
            lock.acquire()
        except AlreadyRunning as exc:
            _fail(str(exc))

        async def run() -> int:
            async with oauth_client(
                credentials=credentials, config=config, stored=stored
            ) as oauth:
                client = TeslaAutoChargeClient(oauth, config)
                await client.build()
                return await run_auto_charge(
                    client,
                    config,
                    dry_run=dry_run,
                    once=once,
                    notifier=notifier,
                )

        try:
            code = asyncio.run(run())
        finally:
            lock.release()
    if code:
        raise SystemExit(code)


def _match_status(local_point: str, other: str | None) -> str:
    point = key_point(other)
    if point is None:
        return "[red]missing or unreadable[/red]"
    if point == local_point:
        return "[green]matches local key[/green]"
    return "[red]does not match local key[/red]"


@cli.group()
def key() -> None:
    """Manage the EC key for partner registration and signed commands."""


@key.command("generate")
@click.option("--pem-only", is_flag=True, help="Print only the public PEM.")
def key_generate(pem_only: bool) -> None:
    """Create the EC P-256 private key and print the public PEM."""
    with _api_errors():
        config = _config()
        path, public_pem = asyncio.run(generate_key(config))
    if pem_only:
        click.echo(public_pem, nl=False)
        return
    console.print(
        f"Private key written to [bold]{escape(str(path))}[/bold] (mode 0600)."
    )
    console.print("Host this public key at (exactly, permanently):")
    target = config.domain or "YOUR_DOMAIN"
    console.print(f"  [bold]https://{escape(target)}{HOSTED_KEY_PATH}[/bold]")
    click.echo(public_pem, nl=False)


@key.command("register")
@click.option(
    "--domain", default=None, help="App root domain (default: config domain)."
)
def key_register(domain: str | None) -> None:
    """Register the partner domain with Tesla; host the key first."""
    with _api_errors():
        config = _config()
        domain = domain or config.domain
        if not domain:
            _fail(
                "no domain set; run 'tesla config set domain <domain>' "
                "or pass --domain"
            )
        credentials = load_credentials()

        async def run() -> dict[str, Any]:
            async with oauth_client(
                credentials=credentials, config=config
            ) as oauth:
                return await register_domain(oauth, credentials, domain)

        result = asyncio.run(run())
    console.print(f"[green]Registered[/green] {escape(domain)} with Tesla.")
    response = result.get("response")
    if response:
        console.print(escape(str(response)))


@key.command("status")
def key_status() -> None:
    """Compare local, Tesla-registered, and hosted public keys."""
    with _api_errors():
        config = _config()
        domain = config.domain
        if not domain:
            _fail("no domain set; run 'tesla config set domain <domain>'")
        credentials = load_credentials()
        _, local_pem = asyncio.run(load_public_key(config))
        local_point = public_point(local_pem)
        if local_point is None:
            _fail("could not read the local public key")

        async def run() -> tuple[str | None, str | None]:
            async with oauth_client(
                credentials=credentials, config=config
            ) as oauth:
                registered = await registered_public_key(
                    oauth, credentials, domain
                )
            hosted = await fetch_hosted_key(domain)
            return registered, hosted

        registered, hosted = asyncio.run(run())
        vin = config.vehicle.vin
        stored = load_tokens()
        fleet_result: dict[str, Any] | None = None
        if vin and stored is not None:

            async def run_fleet() -> dict[str, Any]:
                async with oauth_client(
                    credentials=credentials, config=config, stored=stored
                ) as oauth:
                    await ensure_fresh_token(oauth)
                    vehicle = oauth.vehicles.createFleet(vin)
                    return await vehicle.fleet_status([vin])

            try:
                fleet_result = asyncio.run(run_fleet())
            except TeslaFleetError as exc:
                fleet_result = {"error": str(exc)}

    table = Table(title=f"Key status for {escape(domain)}")
    table.add_column("Source")
    table.add_column("Status")
    table.add_row("local private key", "[green]present[/green]")
    table.add_row("Tesla registered", _match_status(local_point, registered))
    table.add_row("hosted URL", _match_status(local_point, hosted))
    console.print(table)
    if fleet_result is not None:
        console.print(
            "Vehicle fleet status: "
            f"{escape(str(fleet_result.get('response', fleet_result)))}"
        )
    elif vin:
        console.print(
            "Vehicle fleet status: skipped "
            "(no user tokens; run 'tesla auth login')"
        )
    else:
        console.print("Vehicle fleet status: skipped (no VIN configured)")


@cli.group()
def config() -> None:
    """View and update configuration."""


@config.command("show")
def config_show() -> None:
    """Print the effective configuration."""
    click.echo(dump_toml(_config()), nl=False)


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set dotted KEY (e.g. auto_charge.threshold) to VALUE."""
    current = _config()
    try:
        set_dotted(current, key, value)
        save_config(current)
    except ConfigError as exc:
        _fail(str(exc))
    console.print(f"[green]set[/green] {escape(key)} = {escape(value)}")


def main() -> None:
    cli()
