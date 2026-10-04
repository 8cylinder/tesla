"""Powerwall live-status helpers."""

from __future__ import annotations

from typing import Any


def _response(data: dict[str, Any]) -> dict[str, Any]:
    response = data.get("response", data)
    return response if isinstance(response, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def percentage_charged(data: dict[str, Any]) -> float | None:
    """Return battery percentage, falling back to energy_left / pack energy."""
    response = _response(data)
    direct = _number(response.get("percentage_charged"))
    if direct is not None:
        return direct
    energy_left = _number(response.get("energy_left"))
    total_pack = _number(response.get("total_pack_energy"))
    if energy_left is None or total_pack is None or total_pack == 0:
        return None
    return energy_left / total_pack * 100.0


def backup_reserve(data: dict[str, Any]) -> float | None:
    return _number(_response(data).get("backup_reserve_percent"))


def grid_status(data: dict[str, Any]) -> str | None:
    value = _response(data).get("grid_status")
    return value if isinstance(value, str) else None
