"""Logging setup for the CLI."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(
    verbosity: int,
    log_file: Path | None = None,
    console: Console | None = None,
) -> None:
    """Configure root logging: -v -> INFO, -vv -> DEBUG, optional file."""
    level = max(logging.DEBUG, logging.WARNING - 10 * verbosity)
    handler: logging.Handler
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file)
        log_format = _FILE_FORMAT
    else:
        handler = RichHandler(
            console=console or Console(stderr=True),
            rich_tracebacks=True,
            show_path=False,
        )
        log_format = "%(message)s"
    logging.basicConfig(
        level=level,
        format=log_format,
        handlers=[handler],
        force=True,
    )
