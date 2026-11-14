"""Diagnostics for SolarEdge API Connector."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SolarEdgeConfigEntry

TO_REDACT = {"token", "access_token", "refresh_token", "name", "serial_number", "parent_serial"}


def _anonymous(values: dict[str, Any], label: str) -> dict[str, Any]:
    """Replace the serial keys of per-device values with a running label."""
    return {f"{label}_{i}": value for i, value in enumerate(values.values())}


def _stringify(data: dict[str, Any]) -> dict[str, Any]:
    """Render timedeltas readably (the diagnostics encoder does not handle them)."""
    return {
        key: str(value) if isinstance(value, timedelta) else value for key, value in data.items()
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    poll_plan = coordinator.poll_plan
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "ledger": coordinator.ledger.as_dict(),
        "budget": _stringify(asdict(coordinator.budget_config)),
        "plan": _stringify(asdict(poll_plan)) if poll_plan else None,
        "power_calls": coordinator.power_calls,
        "energy_calls": coordinator.energy_calls,
        "status_calls": coordinator.status_calls,
        "capabilities": coordinator.capabilities,
        "last_power": coordinator.last_power,
        "last_energy": coordinator.last_energy,
        "last_status": coordinator.last_status,
        "data_timestamp": coordinator.data_timestamp,
        "counter_cursor": coordinator.counter_cursor,
        "counter_state": coordinator.counter_state,
        "devices": [async_redact_data(asdict(device), TO_REDACT) for device in coordinator.devices],
        "values": coordinator.values,
        "battery_values": _anonymous(coordinator.battery_values, "battery"),
        "battery_status": _anonymous(coordinator.battery_status, "battery"),
        "inverter_status": _anonymous(coordinator.inverter_status, "inverter"),
        "ev_values": _anonymous(coordinator.ev_values, "charger"),
        "sensor_values": _anonymous(coordinator.sensor_values, "sensor"),
        "soc_scales": list(coordinator.soc_scales.values()),
        "raw_storage": _anonymous(coordinator.raw_storage, "battery"),
    }
