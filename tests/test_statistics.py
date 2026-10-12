"""Tests for hourly counter statistics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    # Meter replaced: counter restarts near zero.
    assert accumulate([(h[3], 40.0)], state) == [(h[3], 5340.0)]
    assert state["offset"] == 5300.0
