"""Configuration paths, validation and persistence.

State lives under ``~/.config/tesla`` unless ``TESLA_CONFIG_DIR`` overrides
it. The TOML schema is deliberately small and strict: unknown keys and
wrong types are rejected so typos surface immediately.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMMAND_MODES = ("signed", "fleet")
REGIONS = ("na", "eu", "cn")


class ConfigError(Exception):
    """Raised when configuration is missing, malformed or invalid."""


def config_dir() -> Path:
    override = os.environ.get("TESLA_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "tesla"


def config_path() -> Path:
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


@dataclass
class NotifyConfig:
    enabled: bool = False
    url: str = "https://ntfy.sh"
    priority: int = 3
    tags: str = "tesla"
    min_priority: int = 1
    verbose: bool = False
    on_start: bool = True


@dataclass
class Config:
    region: str = "na"
    domain: str | None = None
    vehicle: VehicleConfig = field(default_factory=VehicleConfig)
    powerwall: PowerwallConfig = field(default_factory=PowerwallConfig)
    auto_charge: AutoChargeConfig = field(default_factory=AutoChargeConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


def key_path(config: Config) -> Path:
    """Resolve the EC private key path (default: config dir)."""
    if config.vehicle.key_path:
        return Path(config.vehicle.key_path).expanduser()
    return config_dir() / "private_key.pem"


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
    },
    "notify": {
        "enabled": bool,
        "url": str,
        "priority": int,
        "tags": str,
        "min_priority": int,
        "verbose": bool,
        "on_start": bool,
    },
}


def _type_name(expected: Any) -> str:
    return "boolean" if expected is bool else expected.__name__


def _check_type(value: Any, expected: Any) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _validate(section: str, data: dict[str, Any], schema: dict[str, Any]) -> None:
    prefix = f"{section}." if section else ""
    for key, value in data.items():
        expected = schema.get(key)
        full = f"{prefix}{key}"
        if expected is None:
            raise ConfigError(f"unknown key {full!r}")
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"{full!r} must be a table")
            _validate(full, value, expected)
        elif not _check_type(value, expected):
            raise ConfigError(
                f"{full!r} must be {_type_name(expected)}, got {type(value).__name__}"
            )


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
    if not 1 <= config.notify.priority <= 5:
        raise ConfigError("notify.priority must be in [1, 5]")
    if not 1 <= config.notify.min_priority <= 5:
        raise ConfigError("notify.min_priority must be in [1, 5]")
    if not config.notify.url:
        raise ConfigError("notify.url must not be empty")


def _build(data: dict[str, Any]) -> Config:
    _validate("", data, _SCHEMA)
    config = Config(region=data.get("region", "na"), domain=data.get("domain"))
    for key, value in (data.get("vehicle") or {}).items():
        setattr(config.vehicle, key, value)
    for key, value in (data.get("powerwall") or {}).items():
        setattr(config.powerwall, key, value)
    for key, value in (data.get("auto_charge") or {}).items():
        setattr(config.auto_charge, key, value)
    for key, value in (data.get("notify") or {}).items():
        setattr(config.notify, key, value)
    _check_values(config)
    return config


def load_config(path: Path | None = None) -> Config:
    """Load and validate config.toml; missing file yields defaults."""
    path = path or config_path()
    if not path.exists():
        return Config()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return _build(data)


def _schema_for(dotted: str) -> Any:
    parts = dotted.split(".")
    if len(parts) == 1:
        expected = _SCHEMA.get(parts[0])
        return expected if isinstance(expected, type) else None
    if len(parts) == 2:
        section = _SCHEMA.get(parts[0])
        if isinstance(section, dict):
            return section.get(parts[1])
    return None


def get_dotted(config: Config, dotted: str) -> Any:
    if _schema_for(dotted) is None:
        raise ConfigError(f"unknown key {dotted!r}")
    target: Any = config
    for part in dotted.split("."):
        target = getattr(target, part)
    return target


def _coerce(raw: str, expected: Any, dotted: str) -> Any:
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


def _toml(value: Any) -> str:
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
        lines.append(f"energy_site_id = {_toml(config.powerwall.energy_site_id)}")
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
        "",
        "[notify]",
        f"enabled = {_toml(config.notify.enabled)}",
        f"url = {_toml(config.notify.url)}",
        f"priority = {_toml(config.notify.priority)}",
        f"tags = {_toml(config.notify.tags)}",
        f"min_priority = {_toml(config.notify.min_priority)}",
        f"verbose = {_toml(config.notify.verbose)}",
        f"on_start = {_toml(config.notify.on_start)}",
    ]
    return "\n".join(lines) + "\n"


def save_config(config: Config, path: Path | None = None) -> None:
    """Write config.toml atomically with mode 0644."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(dump_toml(config))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
