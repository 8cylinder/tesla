"""Tesla Fleet API CLI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tesla")
except PackageNotFoundError:  # pragma: no cover - source tree without metadata
    __version__ = "0.0.0"

__all__ = ["__version__"]
