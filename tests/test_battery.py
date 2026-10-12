"""Tests for battery state-of-charge scale detection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.solaredge_v2.api import DeviceTelemetry
from custom_components.solaredge_v2.coordinator import (
    SOC_FRACTION,
    SOC_PERCENT,
    battery_state,
    detect_soc_scale,
)

TS = datetime(2026, 10, 12, 16, tzinfo=UTC)


@pytest.mark.parametrize(
    ("soe", "remaining", "expected"),
    [
        (1.0, 9_230.0, SOC_FRACTION),  # full 9.7 kWh battery reported as 0-1
        (0.5, 4_850.0, SOC_FRACTION),
        (50.0, 4_850.0, SOC_PERCENT),
        (0.8, 70.0, SOC_PERCENT),  # 0.8 % of a ~9 kWh battery
        (0.0, 0.0, None),  # empty: can't tell
        (0.4, None, None),
    ],
)
def test_detect_soc_scale(soe: float, remaining: float | None, expected: str | None) -> None:
    """The plausible usable capacity decides between 0-1 and 0-100."""
    assert detect_soc_scale(soe, remaining) == expected


def _telemetry(**batteries: tuple[float, float]) -> DeviceTelemetry:
    return DeviceTelemetry(
        devices={
            serial: {
                "stateOfEnergy": [(TS, soe)],
                "remainingEnergy": [(TS, remaining)],
                "chargePower": [(TS, 0.0)],
                "dischargePower": [(TS, 500.0)],
            }
            for serial, (soe, remaining) in batteries.items()
        }
    )


def test_battery_state_two_full_batteries() -> None:
    """Two full batteries reporting 1.0 read 100 % with a realistic capacity."""
    scales: dict[str, str] = {}
    totals, per_battery = battery_state(_telemetry(A=(1.0, 9_230.0), B=(1.0, 9_237.5)), scales)
    assert totals["battery_state_of_charge"] == pytest.approx(100)
    assert totals["battery_usable_capacity"] == pytest.approx(18_467.5)
    assert per_battery["A"]["state_of_charge"] == pytest.approx(100)
    assert per_battery["B"]["power"] == 500.0
    assert scales == {"A": SOC_FRACTION, "B": SOC_FRACTION}


def test_battery_state_uses_remembered_scale_when_empty() -> None:
    """An empty battery can't be classified, so the remembered scale applies."""
    scales = {"A": SOC_PERCENT}
    totals, _ = battery_state(_telemetry(A=(0.0, 0.0)), scales)
    assert totals["battery_state_of_charge"] == 0
    totals, _ = battery_state(_telemetry(A=(1.0, 90.0)), scales)
    assert totals["battery_state_of_charge"] == pytest.approx(1.0)
