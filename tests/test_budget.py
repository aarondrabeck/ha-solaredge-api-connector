"""Tests for the credit planner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.solaredge_v2.budget import (
    BudgetConfig,
    CreditLedger,
    cycle_bounds,
    is_due,
    plan,
)
from custom_components.solaredge_v2.const import MIN_POLL_INTERVAL

TZ = timezone(timedelta(hours=-6))
CONFIG = BudgetConfig(
    monthly_budget=1800, billing_day=1, day_share=0.75, energy_interval=timedelta(hours=3)
)


def _plan(now: datetime, ledger: CreditLedger, *, is_day: bool = True, **kwargs):
    defaults = {"power_calls": 2, "energy_calls": 2, "is_day": is_day, "day_hours": 12.0}
    defaults.update(kwargs)
    return plan(now, CONFIG, ledger, **defaults)


@pytest.mark.parametrize(
    ("now", "billing_day", "start", "end"),
    [
        (datetime(2026, 10, 12, 9, tzinfo=TZ), 1, (2026, 10, 1), (2026, 11, 1)),
        (datetime(2026, 10, 12, 9, tzinfo=TZ), 15, (2026, 9, 15), (2026, 10, 15)),
        (datetime(2026, 12, 20, tzinfo=TZ), 15, (2026, 12, 15), (2027, 1, 15)),
        (datetime(2027, 1, 3, tzinfo=TZ), 15, (2026, 12, 15), (2027, 1, 15)),
        # Anniversary on the 31st clamps to short months.
        (datetime(2026, 2, 28, 12, tzinfo=TZ), 31, (2026, 2, 28), (2026, 3, 31)),
        (datetime(2026, 2, 27, 12, tzinfo=TZ), 31, (2026, 1, 31), (2026, 2, 28)),
    ],
)
def test_cycle_bounds(now, billing_day, start, end) -> None:
    """Billing cycles follow the anniversary day."""
    cycle_start, cycle_end = cycle_bounds(now, billing_day)
    assert cycle_start.timetuple()[:3] == start
    assert cycle_end.timetuple()[:3] == end


def test_ledger_roll_resets_on_new_cycle() -> None:
    """Credits reset when a new cycle starts, not before."""
    ledger = CreditLedger()
    assert ledger.roll(datetime(2026, 10, 2, tzinfo=TZ), 1)
    ledger.record(40)
    ledger.exhausted = True
    assert not ledger.roll(datetime(2026, 10, 31, tzinfo=TZ), 1)
    assert ledger.used == 40
    assert ledger.roll(datetime(2026, 11, 1, 0, 1, tzinfo=TZ), 1)
    assert ledger.used == 0
    assert not ledger.exhausted


def test_ledger_round_trip() -> None:
    """The ledger survives serialization."""
    ledger = CreditLedger()
    ledger.roll(datetime(2026, 10, 2, tzinfo=TZ), 1)
    ledger.record(7)
    assert CreditLedger.from_dict(ledger.as_dict()) == ledger


def test_fresh_cycle_projection_within_budget() -> None:
    """At the start of a cycle the projection fits the budget."""
    now = datetime(2026, 10, 1, 0, 5, tzinfo=TZ)
    result = _plan(now, CreditLedger(cycle_start=cycle_bounds(now, 1)[0]))
    assert result.projected_total <= CONFIG.monthly_budget
    assert result.energy_interval == timedelta(hours=3)
    assert result.power_interval_day < result.power_interval_night
    assert MIN_POLL_INTERVAL <= result.power_interval_day <= timedelta(hours=1)


def test_simulated_cycle_never_exceeds_budget() -> None:
    """Following the plan tick by tick for a whole month stays within budget."""
    start = datetime(2026, 10, 1, tzinfo=TZ)
    ledger = CreditLedger()
    ledger.roll(start, 1)
    now = start
    last_power = last_energy = None
    while now < datetime(2026, 11, 1, tzinfo=TZ):
        is_day = 7 <= now.hour < 19
        result = _plan(now, ledger, is_day=is_day)
        if is_due(last_power, result.power_interval, now):
            ledger.record(2)
            last_power = now
        if is_due(last_energy, result.energy_interval, now):
            ledger.record(2)
            last_energy = now
        now += timedelta(minutes=5)
    assert 1500 <= ledger.used <= CONFIG.monthly_budget


def test_overspend_slows_polling() -> None:
    """Credits burned early (e.g. restarts) lengthen the intervals."""
    now = datetime(2026, 10, 10, 12, tzinfo=TZ)
    cycle_start = cycle_bounds(now, 1)[0]
    normal = _plan(now, CreditLedger(cycle_start=cycle_start, used=520))
    heavy = _plan(now, CreditLedger(cycle_start=cycle_start, used=1200))
    assert heavy.power_interval > normal.power_interval
    assert heavy.projected_total <= CONFIG.monthly_budget


def test_exhausted_or_spent_pauses() -> None:
    """No polling once the budget is gone or the API reported exhaustion."""
    now = datetime(2026, 10, 20, tzinfo=TZ)
    cycle_start = cycle_bounds(now, 1)[0]
    spent = _plan(now, CreditLedger(cycle_start=cycle_start, used=1800))
    assert spent.power_interval is None
    assert spent.energy_interval is None
    exhausted = _plan(now, CreditLedger(cycle_start=cycle_start, used=10, exhausted=True))
    assert exhausted.power_interval is None


def test_energy_capped_when_budget_tight() -> None:
    """Energy counters take at most a share of what is left."""
    now = datetime(2026, 10, 2, tzinfo=TZ)
    cycle_start = cycle_bounds(now, 1)[0]
    result = _plan(now, CreditLedger(cycle_start=cycle_start, used=1650))
    assert result.energy_interval is not None
    assert result.energy_interval > timedelta(hours=3)


def test_is_due() -> None:
    """Due when never run or the interval (minus slack) has passed."""
    now = datetime(2026, 10, 12, 12, tzinfo=TZ)
    assert is_due(None, timedelta(minutes=30), now)
    assert not is_due(None, None, now)
    assert not is_due(now - timedelta(minutes=20), timedelta(minutes=30), now)
    assert is_due(now - timedelta(minutes=29, seconds=30), timedelta(minutes=30), now)
