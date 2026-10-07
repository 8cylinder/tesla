"""Vehicle charge-state helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CHARGING_STATES = (
    "Charging",
    "Complete",
    "Stopped",
    "Disconnected",
    "NoPower",
)

COS_STATE_KEYS = (
    "not_allowed",
    "no_charge_recommended",
    "charging_on_excess_solar",
    "charging_on_anything",
    "user_disabled",
    "waiting_for_server",
    "error",
    "user_stopped",
)

_REASON_PREFIX = "CHARGE_ON_SOLAR_NO_CHARGE_REASON_"


@dataclass(frozen=True)
class VehicleStatus:
    """The fields the Charge on Solar gate needs from one vehicle read."""

    charging_state: str | None = None
    battery_level: int | None = None
    cos_state: str | None = None
    cos_reason: str | None = None


@dataclass(frozen=True)
class ChargeOnSolarSettings:
    """The app-side Charge on Solar setting (get_charge_on_solar)."""

    enabled: bool = False
    lower_limit: float | None = None
    upper_limit: float | None = None


def charge_state(vehicle_data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the charge_state block from a vehicle_data response."""
    response = vehicle_data.get("response", vehicle_data)
    if not isinstance(response, dict):
        return None
    state = response.get("charge_state")
    return state if isinstance(state, dict) else None


def charging_state(vehicle_data: dict[str, Any]) -> str | None:
    state = charge_state(vehicle_data) or {}
    value = state.get("charging_state")
    return value if isinstance(value, str) else None


def is_plugged(vehicle_data: dict[str, Any]) -> bool | None:
    state = charging_state(vehicle_data)
    if state is None:
        return None
    return state != "Disconnected"


def battery_level(vehicle_data: dict[str, Any]) -> int | None:
    state = charge_state(vehicle_data) or {}
    value = state.get("battery_level")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _managed_state(vehicle_data: dict[str, Any]) -> dict[str, Any] | None:
    state = charge_state(vehicle_data) or {}
    managed = state.get("managed_charging_state")
    return managed if isinstance(managed, dict) else None


def charge_on_solar_state(vehicle_data: dict[str, Any]) -> str | None:
    """Live CoS state; accepts both the proto oneof dict and a plain string."""
    managed = _managed_state(vehicle_data) or {}
    value = managed.get("charge_on_solar_state")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return next((key for key in COS_STATE_KEYS if key in value), None)
    return None


def charge_on_solar_reason(vehicle_data: dict[str, Any]) -> str | None:
    """Reason from no_charge_recommended, stripped of the enum prefix."""
    managed = _managed_state(vehicle_data) or {}
    value = managed.get("charge_on_solar_state")
    if not isinstance(value, dict):
        return None
    blocked = value.get("no_charge_recommended")
    if not isinstance(blocked, dict):
        return None
    reason = blocked.get("reason")
    if not isinstance(reason, str):
        return None
    return reason.removeprefix(_REASON_PREFIX)


def vehicle_status(vehicle_data: dict[str, Any]) -> VehicleStatus:
    return VehicleStatus(
        charging_state=charging_state(vehicle_data),
        battery_level=battery_level(vehicle_data),
        cos_state=charge_on_solar_state(vehicle_data),
        cos_reason=charge_on_solar_reason(vehicle_data),
    )


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _find_feature(data: Any) -> dict[str, Any] | None:
    """Find the charge_on_solar feature at any nesting depth of a response."""
    if isinstance(data, dict):
        for key in ("charge_on_solar", "chargeOnSolar"):
            value = data.get(key)
            if isinstance(value, dict) and any(
                marker in value
                for marker in ("enabled", "lower_charge_limit", "lowerChargeLimit")
            ):
                return value
        for value in data.values():
            found = _find_feature(value)
            if found is not None:
                return found
    return None


def charge_on_solar_settings(data: Any) -> ChargeOnSolarSettings | None:
    """Parse get_charge_on_solar(); tolerant of key casing and nesting."""
    feature = _find_feature(data)
    if feature is None:
        return None
    enabled = feature.get("enabled")
    return ChargeOnSolarSettings(
        enabled=enabled if isinstance(enabled, bool) else False,
        lower_limit=_number(
            feature.get("lower_charge_limit", feature.get("lowerChargeLimit"))
        ),
        upper_limit=_number(
            feature.get("upper_charge_limit", feature.get("upperChargeLimit"))
        ),
    )
