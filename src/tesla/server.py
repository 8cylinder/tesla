"""Read-only inspection of a remote Docker deployment."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

CONTAINER = "tesla-app"
SSH_CONNECT_TIMEOUT = 10
INFO_TIMEOUT = 30
_DOCKER_FRACTION = re.compile(r"(\.\d{6})\d+")


class ServerError(Exception):
    """Raised when the remote server cannot be inspected."""


@dataclass(frozen=True)
class Mount:
    source: str
    destination: str
    mode: str
    read_write: bool


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    status: str
    image: str
    tag: str
    command: str
    mounts: tuple[Mount, ...]
    started_at: datetime | None
    exit_code: int | None


def validate_url(url: str) -> str:
    """Validate and normalize an SSH destination."""
    target = url.strip()
    if not target:
        raise ServerError("server URL is empty")
    if target.startswith("-"):
        raise ServerError(f"invalid server URL {url!r}")
    if any(char.isspace() for char in target):
        raise ServerError(f"invalid server URL {url!r}")
    return target


def _ssh_argv(url: str, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        url,
        remote_command,
    ]


def _stderr(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or "").strip()
    return detail.splitlines()[-1] if detail else f"exit {result.returncode}"


def fetch_info(url: str) -> ContainerInfo:
    """Return container details from ``docker inspect`` over SSH."""
    target = validate_url(url)
    command = shlex.join(["docker", "inspect", CONTAINER])
    try:
        result = subprocess.run(
            _ssh_argv(target, command),
            capture_output=True,
            text=True,
            timeout=INFO_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ServerError("ssh not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ServerError(f"timed out contacting {target}") from exc
    except OSError as exc:
        raise ServerError(f"cannot run ssh: {exc}") from exc
    if result.returncode == 255:
        raise ServerError(f"cannot reach {target}: {_stderr(result)}")
    if result.returncode != 0:
        detail = _stderr(result)
        if "No such object" in detail or "No such container" in detail:
            raise ServerError(f"container {CONTAINER!r} not found on {target}")
        raise ServerError(f"docker inspect failed on {target}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ServerError(
            f"unexpected response from docker inspect on {target}"
        ) from exc
    if (
        not isinstance(payload, list)
        or not payload
        or not isinstance(payload[0], dict)
    ):
        raise ServerError(
            f"unexpected response from docker inspect on {target}"
        )
    return container_info(payload[0])


def container_info(data: dict[str, Any]) -> ContainerInfo:
    """Build a :class:`ContainerInfo` from one inspect object."""
    state = data.get("State")
    config = data.get("Config")
    state = state if isinstance(state, dict) else {}
    config = config if isinstance(config, dict) else {}
    name = str(data.get("Name") or CONTAINER).lstrip("/")
    status = str(state.get("Status") or "unknown")
    image_ref = str(config.get("Image") or "")
    image, tag = split_image_tag(image_ref)
    entrypoint = _string_list(config.get("Entrypoint"))
    cmd = _string_list(config.get("Cmd"))
    command = shlex.join([*entrypoint, *cmd]) or "<none>"
    mounts = tuple(
        _mount(item)
        for item in (data.get("Mounts") or [])
        if isinstance(item, dict)
    )
    exit_code = state.get("ExitCode")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None
    return ContainerInfo(
        name=name,
        status=status,
        image=image,
        tag=tag,
        command=command,
        mounts=mounts,
        started_at=_parse_time(state.get("StartedAt")),
        exit_code=exit_code,
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _mount(data: dict[str, Any]) -> Mount:
    return Mount(
        source=str(data.get("Source") or data.get("Name") or ""),
        destination=str(data.get("Destination") or ""),
        mode=str(data.get("Mode") or ""),
        read_write=bool(data.get("RW", True)),
    )


def split_image_tag(reference: str) -> tuple[str, str]:
    """Split an image reference into image and tag (or digest)."""
    if not reference:
        return "", ""
    if "@" in reference:
        image, _, digest = reference.partition("@")
        return image, digest
    image, _, tag = reference.rpartition(":")
    if not image or "/" in tag:
        return reference, ""
    return image, tag


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("0001-01-01"):
        return None
    cleaned = _DOCKER_FRACTION.sub(r"\1", value)
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def format_uptime(
    started_at: datetime | None,
    *,
    now: datetime | None = None,
) -> str:
    """Format the time since the container started."""
    if started_at is None:
        return "unknown"
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    seconds = max(0, int((current - started_at).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def stream_logs(
    url: str,
    *,
    follow: bool,
    tail: int | None,
) -> int:
    """Run ``docker logs`` over SSH and return its exit code."""
    target = validate_url(url)
    parts = ["docker", "logs"]
    if follow:
        parts.append("--follow")
    if tail is not None:
        parts.extend(["--tail", str(tail)])
    parts.append(CONTAINER)
    try:
        result = subprocess.run(
            _ssh_argv(target, shlex.join(parts)),
            check=False,
        )
    except FileNotFoundError as exc:
        raise ServerError("ssh not found in PATH") from exc
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise ServerError(f"cannot run ssh: {exc}") from exc
    if result.returncode == 255:
        raise ServerError(f"cannot reach {target}")
    return result.returncode
