"""Hourly external statistics from SolarEdge lifetime energy counters.

The counters carry their own 15-minute timestamps, so each kWh lands in the
hour it was produced, however late or rarely we poll. Because a counter is
already cumulative, it serves directly as the statistic ``sum``; re-importing
an hour is idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter

from .api import Series
from .const import DOMAIN

STATISTIC_NAMES: dict[str, str] = {
    "solar": "Solar production",
    "home": "Home consumption",
    "grid_import": "Grid import",
    "grid_export": "Grid export",
    "battery_charge": "Battery charge",
    "battery_discharge": "Battery discharge",
}

# Readings below the previous one by more than this are treated as a counter reset.
RESET_TOLERANCE_WH = 1.0


def statistic_id(site_id: int, key: str) -> str:
    """Return the external statistic id for a counter."""
    return f"{DOMAIN}:{site_id}_{key}"


def _hour(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hourly_totals(series_by_device: dict[str, Series]) -> list[tuple[datetime, float]]:
    """Sum device counters at the end of each complete UTC hour.

    An hour is complete once every device has a reading in a later hour. The
    last reading inside an hour stands for that hour's closing value.
    """
    per_device: dict[str, dict[datetime, float]] = {}
    for serial, series in series_by_device.items():
        hourly: dict[datetime, float] = {}
        for ts, value in sorted(series, key=lambda point: point[0]):
            hourly[_hour(ts)] = value
        if hourly:
            per_device[serial] = hourly
    if not per_device:
        return []

    cutoff = min(max(hourly) for hourly in per_device.values())
    hours = sorted({hour for hourly in per_device.values() for hour in hourly})
    carry: dict[str, float] = {}
    totals: list[tuple[datetime, float]] = []
    for hour in hours:
        if hour >= cutoff:
            break
        for serial, hourly in per_device.items():
            if hour in hourly:
                carry[serial] = hourly[hour]
        if len(carry) == len(per_device):
            totals.append((hour, sum(carry.values())))
    return totals


def accumulate(
    hourly: list[tuple[datetime, float]], state: dict[str, Any]
) -> list[tuple[datetime, float]]:
    """Turn hourly counter totals into new statistic sums, updating ``state``.

    ``state`` holds ``last_hour``, ``last_raw`` and ``offset`` so hours already
    imported are skipped and a counter reset (e.g. a replaced meter) does not
    produce negative energy.
    """
    last_hour = datetime.fromisoformat(state["last_hour"]) if state.get("last_hour") else None
    last_raw: float | None = state.get("last_raw")
    offset: float = state.get("offset", 0.0)
    rows: list[tuple[datetime, float]] = []
    for hour, raw in hourly:
        if last_hour is not None and hour <= last_hour:
            continue
        if last_raw is not None and raw < last_raw - RESET_TOLERANCE_WH:
            offset += last_raw
        last_raw, last_hour = raw, hour
        rows.append((hour, raw + offset))
    if last_hour is not None:
        state.update(last_hour=last_hour.isoformat(), last_raw=last_raw, offset=offset)
    return rows


def async_import_counters(
    hass: HomeAssistant,
    site_id: int,
    site_name: str,
    counters: dict[str, dict[str, Series]],
    state: dict[str, dict[str, Any]],
) -> None:
    """Import new hourly sums for each counter key into the recorder."""
    for key, series_by_device in counters.items():
        rows = accumulate(hourly_totals(series_by_device), state.setdefault(key, {}))
        if not rows:
            continue
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{site_name} {STATISTIC_NAMES.get(key, key)}",
            source=DOMAIN,
            statistic_id=statistic_id(site_id, key),
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        async_add_external_statistics(
            hass,
            metadata,
            [
                StatisticData(start=hour, state=total / 1000, sum=total / 1000)
                for hour, total in rows
            ],
        )
