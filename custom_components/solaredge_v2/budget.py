"""Credit budgeting: spread a monthly API allowance over the billing cycle.

Pure logic with no Home Assistant imports so it can be unit tested directly.
Polling intervals are derived from what is left of the budget, so restarts or
manual refreshes slow polling down instead of overspending.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import MIN_POLL_INTERVAL

# Site metadata + inventory, refreshed weekly.
INVENTORY_CALLS = 2
# Energy counters may take at most this share of the remaining budget.
ENERGY_MAX_SHARE = 0.4
# Ticks are coarse, so a group counts as due slightly early.
DUE_SLACK = timedelta(minutes=1)


@dataclass(slots=True)
class BudgetConfig:
    """User options that drive the planner."""

    monthly_budget: int
    billing_day: int
    day_share: float
    energy_interval: timedelta


@dataclass(slots=True)
class PollPlan:
    """Intervals for the current moment; ``None`` means paused."""

    power_interval: timedelta | None
    power_interval_day: timedelta | None
    power_interval_night: timedelta | None
    energy_interval: timedelta | None
    projected_total: int
    cycle_end: datetime


def _anchor(reference: datetime, year: int, month: int, billing_day: int) -> datetime:
    day = min(billing_day, monthrange(year, month)[1])
    return reference.replace(
        year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0
    )


def cycle_bounds(now: datetime, billing_day: int) -> tuple[datetime, datetime]:
    """Return the billing cycle containing ``now`` (anniversary day, clamped)."""
    this_month = _anchor(now, now.year, now.month, billing_day)
    if now >= this_month:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return this_month, _anchor(now, year, month, billing_day)
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return _anchor(now, year, month, billing_day), this_month


@dataclass(slots=True)
class CreditLedger:
    """Credits spent in the current billing cycle (persisted)."""

    cycle_start: datetime | None = None
    used: int = 0
    exhausted: bool = False

    def roll(self, now: datetime, billing_day: int) -> bool:
        """Reset when a new cycle starts; return True if it did."""
        start, _ = cycle_bounds(now, billing_day)
        if self.cycle_start is not None and self.cycle_start == start:
            return False
        self.cycle_start = start
        self.used = 0
        self.exhausted = False
        return True

    def record(self, credits: int = 1) -> None:
        """Count credits for calls that reached the API."""
        self.used += credits

    def as_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {
            "cycle_start": self.cycle_start.isoformat() if self.cycle_start else None,
            "used": self.used,
            "exhausted": self.exhausted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CreditLedger:
        """Restore from storage."""
        if not data:
            return cls()
        start = data.get("cycle_start")
        return cls(
            cycle_start=datetime.fromisoformat(start) if start else None,
            used=int(data.get("used", 0)),
            exhausted=bool(data.get("exhausted", False)),
        )


def plan(
    now: datetime,
    config: BudgetConfig,
    ledger: CreditLedger,
    *,
    power_calls: int,
    energy_calls: int,
    is_day: bool,
    day_hours: float,
) -> PollPlan:
    """Work out polling intervals that keep the cycle within budget."""
    _, cycle_end = cycle_bounds(now, config.billing_day)
    remaining = max(config.monthly_budget - ledger.used, 0)
    if ledger.exhausted or remaining <= 0:
        return PollPlan(None, None, None, None, ledger.used, cycle_end)

    secs_left = max((cycle_end - now).total_seconds(), 3600.0)
    days_left = secs_left / 86400
    reserve = INVENTORY_CALLS * days_left / 7
    usable = max(remaining - reserve, 0.0)

    energy_interval: timedelta | None = None
    energy_spend = 0.0
    if energy_calls:
        wanted = energy_calls * secs_left / config.energy_interval.total_seconds()
        cap = usable * ENERGY_MAX_SHARE
        if wanted <= cap:
            energy_interval, energy_spend = config.energy_interval, wanted
        elif cap >= energy_calls:
            energy_interval = timedelta(seconds=energy_calls * secs_left / cap)
            energy_spend = cap
        energy_interval = _floor(energy_interval)

    day_h = min(max(day_hours, 1.0), 23.0)
    night_h = 24.0 - day_h
    power_budget = usable - energy_spend
    interval_day = interval_night = None
    if power_calls and power_budget >= power_calls:
        cycles_per_day = power_budget / power_calls / days_left
        interval_day = _floor(timedelta(hours=day_h / max(cycles_per_day * config.day_share, 1e-9)))
        interval_night = _floor(
            timedelta(hours=night_h / max(cycles_per_day * (1 - config.day_share), 1e-9))
        )

    per_day = 0.0
    if interval_day and interval_night:
        per_day += power_calls * (
            day_h * 3600 / interval_day.total_seconds()
            + night_h * 3600 / interval_night.total_seconds()
        )
    if energy_interval:
        per_day += energy_calls * 86400 / energy_interval.total_seconds()
    projected = round(ledger.used + per_day * days_left + reserve)

    return PollPlan(
        power_interval=interval_day if is_day else interval_night,
        power_interval_day=interval_day,
        power_interval_night=interval_night,
        energy_interval=energy_interval,
        projected_total=projected,
        cycle_end=cycle_end,
    )


def _floor(interval: timedelta | None) -> timedelta | None:
    if interval is None:
        return None
    return max(interval, MIN_POLL_INTERVAL)


def is_due(last: datetime | None, interval: timedelta | None, now: datetime) -> bool:
    """Return True when a group should be fetched now."""
    if interval is None:
        return False
    return last is None or now - last >= interval - DUE_SLACK
