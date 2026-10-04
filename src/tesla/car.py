"""Vehicle charge-state helpers."""

from __future__ import annotations

from typing import Any

CHARGING_STATES = (
    "Charging",
    "Complete",
    "Stopped",
    "Disconnected",
    "NoPower",
)


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
