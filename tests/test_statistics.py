"""Tests for hourly counter statistics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.solaredge_v2.coordinator import steady_total
from custom_components.solaredge_v2.statistics import accumulate, hourly_totals

T0 = datetime(2026, 10, 12, 10, 3, tzinfo=UTC)


def _series(values: list[float], start: datetime = T0) -> list[tuple[datetime, float]]:
    return [(start + timedelta(minutes=15 * i), v) for i, v in enumerate(values)]


def test_hourly_totals_last_reading_per_complete_hour() -> None:
    """Each complete hour closes on its last reading; the open hour is skipped."""
    # 10:03 10:18 10:33 10:48 | 11:03 11:18 11:33 11:48 | 12:03
    series = _series([100, 110, 120, 130, 140, 150, 160, 170, 180])
    assert hourly_totals({"m1": series}) == [
        (datetime(2026, 10, 12, 10, tzinfo=UTC), 130),
        (datetime(2026, 10, 12, 11, tzinfo=UTC), 170),
    ]


def test_hourly_totals_sums_devices_with_carry_forward() -> None:
    """Multiple meters are summed; a missing reading carries the previous one."""
    m1 = _series([100, 110, 120, 130, 140, 150, 160, 170, 180])
    m2 = [(T0, 1000.0), (T0 + timedelta(hours=2), 1100.0)]
    assert hourly_totals({"m1": m1, "m2": m2}) == [
        (datetime(2026, 10, 12, 10, tzinfo=UTC), 1130),
        (datetime(2026, 10, 12, 11, tzinfo=UTC), 1170),
    ]


def test_hourly_totals_empty() -> None:
    """No readings, no statistics."""
    assert hourly_totals({}) == []
    assert hourly_totals({"m1": []}) == []


def test_accumulate_skips_imported_hours_and_handles_reset() -> None:
    """Re-imports are idempotent and counter resets don't go negative."""
    state: dict = {}
    h = [datetime(2026, 10, 12, hour, tzinfo=UTC) for hour in range(10, 14)]
    assert accumulate([(h[0], 5000.0), (h[1], 5200.0)], state) == [
        (h[0], 5000.0),
        (h[1], 5200.0),
    ]
    # Overlapping window: hour 11 already imported.
    assert accumulate([(h[1], 5200.0), (h[2], 5300.0)], state) == [(h[2], 5300.0)]
    # Meter replaced: counter restarts near zero, and the sum holds for that hour.
    assert accumulate([(h[3], 40.0)], state) == [(h[3], 5300.0)]
    assert state["offset"] == 5260.0


def test_accumulate_battery_counter_stepping_back_does_not_spike() -> None:
    """Battery counters really do step back; that must not add the whole counter again."""
    h = [datetime(2026, 10, 12, hour, tzinfo=UTC) for hour in range(10, 14)]
    state: dict = {}
    # Readings from a live site: the counter fell 2.045 kWh, then carried on from there.
    rows = accumulate(
        [(h[0], 8_215_655.0), (h[1], 8_213_610.0), (h[2], 8_213_675.0), (h[3], 8_214_000.0)],
        state,
    )
    assert [total for _, total in rows] == [8_215_655.0, 8_215_655.0, 8_215_720.0, 8_216_045.0]


def test_accumulate_one_of_several_devices_replaced() -> None:
    """Losing one device's share of the total does not count the rest twice."""
    h = [datetime(2026, 10, 12, hour, tzinfo=UTC) for hour in range(10, 12)]
    state: dict = {}
    rows = accumulate([(h[0], 10_000.0), (h[1], 6_000.0)], state)
    assert [total for _, total in rows] == [10_000.0, 10_000.0]


@pytest.mark.parametrize(
    ("previous", "total", "expected"),
    [
        (None, 100.0, 100.0),
        (100.0, 120.0, 120.0),
        # A small step back is held so the sensor stays total_increasing.
        (5_790_984.0, 5_790_982.0, 5_790_984.0),
        # A drop over 10 % is a real reset, which Home Assistant handles itself.
        (5_790_984.0, 40.0, 40.0),
    ],
)
def test_steady_total(previous: float | None, total: float, expected: float) -> None:
    """Lifetime sensors ride out dips but still follow a real reset."""
    assert steady_total(previous, total) == expected
