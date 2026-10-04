"""Refresh-token persistence: atomic writes with mode 0600."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import config_dir, tokens_path


class TokenError(Exception):
    """Raised when the token file is missing, unreadable or insecure."""


@dataclass
class Tokens:
    refresh_token: str
    expires: int


def load_tokens(path: Path | None = None) -> Tokens | None:
    """Return stored tokens, or None when no token file exists."""
    path = path or tokens_path()
    if not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise TokenError(
            f"{path} is mode {mode:04o}; expected 0600 "
            "(run: chmod 600, or re-run 'tesla auth login')"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenError(f"cannot read {path}: {exc}") from exc
    refresh_token = data.get("refresh_token")
    expires = data.get("expires")
    if not isinstance(refresh_token, str) or not isinstance(expires, int):
        raise TokenError(f"{path} must contain refresh_token and expires")
    return Tokens(refresh_token=refresh_token, expires=expires)


def save_tokens(tokens: Tokens, path: Path | None = None) -> None:
    """Write tokens atomically with mode 0600."""
    path = path or tokens_path()
    config_dir().mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"refresh_token": tokens.refresh_token, "expires": tokens.expires}
    )
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tokens-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
