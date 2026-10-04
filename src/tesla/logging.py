"""Logging setup for the CLI."""

from __future__ import annotations

import logging
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(verbosity: int, log_file: Path | None = None) -> None:
    """Configure root logging: -v -> INFO, -vv -> DEBUG, optional file."""
    level = max(logging.DEBUG, logging.WARNING - 10 * verbosity)
    handler: logging.Handler
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file)
    else:
        handler = logging.StreamHandler()
    logging.basicConfig(level=level, format=_FORMAT, handlers=[handler], force=True)
