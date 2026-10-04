"""Click command groups for the tesla CLI."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .config import ConfigError, dump_toml, load_config, save_config, set_dotted
from .logging import setup_logging

console = Console()
err_console = Console(stderr=True)


def _fail(message: str, code: int = 2) -> None:
    err_console.print(f"[red]error:[/red] {message}")
    raise SystemExit(code)


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
