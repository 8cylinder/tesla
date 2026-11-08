"""Configuration paths, validation and persistence.

State lives in the platform per-user config directory (``platformdirs``).
The TOML schema is deliberately small and strict: wrong types are rejected
so typos surface immediately. Unknown keys are warned about and ignored so
older config files keep working after an upgrade.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

APP = "tesla"
COMMAND_MODES = ("signed", "fleet")
REGIONS = ("na", "eu", "cn")
NOTIFY_SERVICES = ("ntfy", "pushover")


class ConfigError(Exception):
    """Raised when configuration is missing, malformed or invalid."""


def config_dir() -> Path:
    """Return the platform-specific per-user config directory."""
    return Path(platformdirs.user_config_dir(APP)).expanduser().absolute()


def config_path() -> Path:
    """Return the absolute path of ``config.toml``."""
    return config_dir() / "config.toml"


def tokens_path() -> Path:
    return config_dir() / "tokens.json"


def lock_path() -> Path:
    return config_dir() / "auto-charge.lock"


def notify_state_path() -> Path:
    return config_dir() / "notify_state.json"


@dataclass
class VehicleConfig:
    vin: str | None = None
    command_mode: str = "signed"
    key_path: str | None = None


@dataclass
class PowerwallConfig:
    energy_site_id: int | None = None


@dataclass
class AutoChargeConfig:
    threshold: float = 100.0
    poll_interval_seconds: int = 300
    require_plugged: bool = True
    wake_timeout_seconds: int = 90
    set_charge_limit: int = 0
    max_runtime_hours: float = 0.0
    charge_on_solar: bool = False
    stop_margin: float = 5.0


@dataclass
class NtfyConfig:
    url: str = "https://ntfy.sh"


@dataclass
class PushoverConfig:
    url: str = "https://api.pushover.net/1/messages.json"
    device: str = ""
    sound: str = ""


@dataclass
class NotifyConfig:
    enabled: bool = False
    service: str = "ntfy"
    priority: int = 3
    tags: str = "tesla"
    min_priority: int = 1
    verbose: bool = False
    on_start: bool = True
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    pushover: PushoverConfig = field(default_factory=PushoverConfig)


@dataclass
class ServerConfig:
    url: str | None = None


@dataclass
class Config:
    region: str = "na"
    domain: str | None = None
    vehicle: VehicleConfig = field(default_factory=VehicleConfig)
    powerwall: PowerwallConfig = field(default_factory=PowerwallConfig)
    auto_charge: AutoChargeConfig = field(default_factory=AutoChargeConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    source_path: Path | None = field(default=None, repr=False, compare=False)


def key_path(config: Config) -> Path:
    """Resolve the EC private key path (default: config dir).

    Relative values from ``config.toml`` are resolved against the config
    file's parent directory, not the process working directory.
    """
    raw = config.vehicle.key_path
    if not raw:
        return config_dir() / "private_key.pem"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = config.source_path.parent if config.source_path else Path.cwd()
        path = base / path
    return path


_SCHEMA: dict[str, Any] = {
    "region": str,
    "domain": str,
    "vehicle": {
        "vin": str,
        "command_mode": str,
        "key_path": str,
    },
    "powerwall": {
        "energy_site_id": int,
    },
    "auto_charge": {
        "threshold": float,
        "poll_interval_seconds": int,
        "require_plugged": bool,
        "wake_timeout_seconds": int,
        "set_charge_limit": int,
        "max_runtime_hours": float,
        "charge_on_solar": bool,
        "stop_margin": float,
    },
    "notify": {
        "enabled": bool,
        "service": str,
        "priority": int,
        "tags": str,
        "min_priority": int,
        "verbose": bool,
        "on_start": bool,
        "ntfy": {
            "url": str,
        },
        "pushover": {
            "url": str,
            "device": str,
            "sound": str,
        },
    },
    "server": {
        "url": str,
    },
}


def _type_name(expected: Any) -> str:  # noqa: ANN401
    return "boolean" if expected is bool else str(expected.__name__)


def _check_type(  # noqa: ANN401
    value: Any,  # noqa: ANN401
    expected: Any,  # noqa: ANN401
) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _clean(
    section: str,
    data: dict[str, Any],
    schema: dict[str, Any],
    unknown: list[str],
) -> dict[str, Any]:
    prefix = f"{section}." if section else ""
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        expected = schema.get(key)
        full = f"{prefix}{key}"
        if expected is None:
            unknown.append(full)
            continue
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"{full!r} must be a table")
            cleaned[key] = _clean(full, value, expected, unknown)
        elif not _check_type(value, expected):
            raise ConfigError(
                f"{full!r} must be {_type_name(expected)}, "
                f"got {type(value).__name__}"
            )
        else:
            cleaned[key] = value
    return cleaned


def _check_values(config: Config) -> None:
    if config.region not in REGIONS:
        regions = ", ".join(REGIONS)
        raise ConfigError(f"region must be one of: {regions}")
    if config.vehicle.command_mode not in COMMAND_MODES:
        modes = ", ".join(COMMAND_MODES)
        raise ConfigError(f"vehicle.command_mode must be one of: {modes}")
    if not 0 < config.auto_charge.threshold <= 100:
        raise ConfigError("auto_charge.threshold must be in (0, 100]")
    if config.auto_charge.poll_interval_seconds <= 0:
        raise ConfigError("auto_charge.poll_interval_seconds must be positive")
    if config.auto_charge.wake_timeout_seconds <= 0:
        raise ConfigError("auto_charge.wake_timeout_seconds must be positive")
    if not 0 <= config.auto_charge.set_charge_limit <= 100:
        raise ConfigError("auto_charge.set_charge_limit must be in [0, 100]")
    if config.auto_charge.max_runtime_hours < 0:
        raise ConfigError("auto_charge.max_runtime_hours must be >= 0")
    if config.auto_charge.stop_margin < 0:
        raise ConfigError("auto_charge.stop_margin must be >= 0")
    if config.auto_charge.stop_margin >= config.auto_charge.threshold:
        raise ConfigError(
            "auto_charge.stop_margin must be less than auto_charge.threshold"
        )
    if config.notify.service not in NOTIFY_SERVICES:
        services = ", ".join(NOTIFY_SERVICES)
        raise ConfigError(f"notify.service must be one of: {services}")
    if not 1 <= config.notify.priority <= 5:
        raise ConfigError("notify.priority must be in [1, 5]")
    if not 1 <= config.notify.min_priority <= 5:
        raise ConfigError("notify.min_priority must be in [1, 5]")
    if not config.notify.ntfy.url:
        raise ConfigError("notify.ntfy.url must not be empty")
    if not config.notify.pushover.url:
        raise ConfigError("notify.pushover.url must not be empty")
    if config.server.url is not None and not config.server.url.strip():
        raise ConfigError("server.url must not be empty")


def _apply(target: Any, values: dict[str, Any]) -> None:  # noqa: ANN401
    for key, value in values.items():
        current = getattr(target, key)
        if isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(target, key, value)


def _build(
    data: dict[str, Any],
    path: Path,
    unknown: list[str],
) -> Config:
    data = dict(data)
    notify_raw = data.get("notify")
    legacy_url: str | None = None
    if isinstance(notify_raw, dict) and "url" in notify_raw:
        # Older configs kept the ntfy URL directly under [notify].
        notify_raw = dict(notify_raw)
        raw_url = notify_raw.pop("url")
        if not isinstance(raw_url, str):
            raise ConfigError(
                f"'notify.url' must be {_type_name(str)}, "
                f"got {type(raw_url).__name__}"
            )
        legacy_url = raw_url
        data["notify"] = notify_raw
    cleaned = _clean("", data, _SCHEMA, unknown)
    config = Config(
        region=cleaned.get("region", "na"),
        domain=cleaned.get("domain"),
        source_path=path,
    )
    if legacy_url is not None:
        config.notify.ntfy.url = legacy_url
    _apply(config, cleaned)
    _check_values(config)
    return config


def load_config(
    path: Path | None = None,
    *,
    on_unknown: Callable[[str], None] | None = None,
) -> Config:
    """Load and validate config.toml; a missing file yields defaults.

    Unknown keys are reported through ``on_unknown`` and ignored. Invalid
    TOML and wrong types raise :class:`ConfigError` naming the file.
    """
    path = path or config_path()
    if not path.exists():
        return Config(source_path=path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    unknown: list[str] = []
    config = _build(data, path, unknown)
    if on_unknown is not None:
        for key in unknown:
            on_unknown(key)
    return config


def _schema_for(dotted: str) -> Any:  # noqa: ANN401
    node: Any = _SCHEMA
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, type) else None


def get_dotted(config: Config, dotted: str) -> Any:  # noqa: ANN401
    if _schema_for(dotted) is None:
        raise ConfigError(f"unknown key {dotted!r}")
    target: Any = config
    for part in dotted.split("."):
        target = getattr(target, part)
    return target


def _coerce(  # noqa: ANN401
    raw: str,
    expected: Any,  # noqa: ANN401
    dotted: str,
) -> Any:  # noqa: ANN401
    if expected is bool:
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        raise ConfigError(f"{dotted} must be a boolean")
    try:
        return expected(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{dotted}: cannot parse {raw!r} as {_type_name(expected)}"
        ) from exc


def set_dotted(config: Config, dotted: str, raw: str) -> None:
    expected = _schema_for(dotted)
    if expected is None:
        raise ConfigError(f"unknown key {dotted!r}")
    parts = dotted.split(".")
    section: Any = config
    for part in parts[:-1]:
        section = getattr(section, part)
    setattr(section, parts[-1], _coerce(raw, expected, dotted))
    _check_values(config)


def _toml(value: Any) -> str:  # noqa: ANN401
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def dump_toml(config: Config) -> str:
    """Render the config as TOML, omitting unset optional values."""
    lines = [f"region = {_toml(config.region)}"]
    if config.domain is not None:
        lines.append(f"domain = {_toml(config.domain)}")
    lines += ["", "[vehicle]"]
    if config.vehicle.vin is not None:
        lines.append(f"vin = {_toml(config.vehicle.vin)}")
    lines.append(f"command_mode = {_toml(config.vehicle.command_mode)}")
    if config.vehicle.key_path is not None:
        lines.append(f"key_path = {_toml(config.vehicle.key_path)}")
    lines += ["", "[powerwall]"]
    if config.powerwall.energy_site_id is not None:
        lines.append(
            f"energy_site_id = {_toml(config.powerwall.energy_site_id)}"
        )
    auto = config.auto_charge
    lines += [
        "",
        "[auto_charge]",
        f"threshold = {_toml(auto.threshold)}",
        f"poll_interval_seconds = {_toml(auto.poll_interval_seconds)}",
        f"require_plugged = {_toml(auto.require_plugged)}",
        f"wake_timeout_seconds = {_toml(auto.wake_timeout_seconds)}",
        f"set_charge_limit = {_toml(auto.set_charge_limit)}",
        f"max_runtime_hours = {_toml(auto.max_runtime_hours)}",
        f"charge_on_solar = {_toml(auto.charge_on_solar)}",
        f"stop_margin = {_toml(auto.stop_margin)}",
        "",
        "[notify]",
        f"enabled = {_toml(config.notify.enabled)}",
        f"service = {_toml(config.notify.service)}",
        f"priority = {_toml(config.notify.priority)}",
        f"tags = {_toml(config.notify.tags)}",
        f"min_priority = {_toml(config.notify.min_priority)}",
        f"verbose = {_toml(config.notify.verbose)}",
        f"on_start = {_toml(config.notify.on_start)}",
        "",
        "[notify.ntfy]",
        f"url = {_toml(config.notify.ntfy.url)}",
        "",
        "[notify.pushover]",
        f"url = {_toml(config.notify.pushover.url)}",
    ]
    if config.notify.pushover.device:
        lines.append(f"device = {_toml(config.notify.pushover.device)}")
    if config.notify.pushover.sound:
        lines.append(f"sound = {_toml(config.notify.pushover.sound)}")
    lines += ["", "[server]"]
    if config.server.url is not None:
        lines.append(f"url = {_toml(config.server.url)}")
    return "\n".join(lines) + "\n"


def save_config(config: Config, path: Path | None = None) -> None:
    """Write config.toml atomically with mode 0600."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=".config-", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(dump_toml(config))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
