"""Credit-aware data coordinator for SolarEdge API Connector."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    OAuth2TokenRequestBaseError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.sun import get_astral_event_date, is_up
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CounterReadings,
    Device,
    DeviceTelemetry,
    Series,
    SiteInfo,
    SolarEdgeAuthError,
    SolarEdgeClient,
    SolarEdgeCreditLimitError,
    SolarEdgeError,
    SolarEdgeForbiddenError,
    SolarEdgeRateLimitError,
)
from .budget import BudgetConfig, CreditLedger, PollPlan, is_due, plan
from .const import (
    CONF_BILLING_DAY,
    CONF_DAY_SHARE,
    CONF_ENERGY_INTERVAL,
    CONF_MONTHLY_BUDGET,
    CONF_SITE_ID,
    COUNTER_BACKFILL,
    COUNTER_MAX_WINDOW,
    DEFAULT_BILLING_DAY,
    DEFAULT_DAY_SHARE,
    DEFAULT_ENERGY_INTERVAL,
    DEFAULT_MONTHLY_BUDGET,
    DOMAIN,
    INVENTORY_REFRESH,
    MONITORING_URL,
    STORAGE_VERSION,
    TELEMETRY_LOOKBACK,
    TICK_INTERVAL,
)
from .statistics import async_import_counters

if TYPE_CHECKING:
    from . import SolarEdgeConfigEntry

_LOGGER = logging.getLogger(__name__)

METER_POWER_KEYS = {
    "productionPower": "solar_power",
    "consumptionPower": "home_power",
    "importPower": "grid_import_power",
    "exportPower": "grid_export_power",
}
METER_COUNTER_KEYS = {
    "productionEnergy": "solar",
    "consumptionEnergy": "home",
    "importEnergy": "grid_import",
    "exportEnergy": "grid_export",
}
STORAGE_COUNTER_KEYS = {
    "lifetimeEnergyCharged": "battery_charge",
    "lifetimeEnergyDischarged": "battery_discharge",
}
FAILURE_BACKOFF_MIN = timedelta(minutes=15)
FAILURE_BACKOFF_MAX = timedelta(hours=3)


def latest_total(telemetry: DeviceTelemetry, metric: str) -> tuple[datetime, float] | None:
    """Sum the latest value of ``metric`` across devices."""
    points = [
        series[-1] for metrics in telemetry.devices.values() if (series := metrics.get(metric))
    ]
    if not points:
        return None
    return max(ts for ts, _ in points), sum(value for _, value in points)


SOC_FRACTION = "fraction"
SOC_PERCENT = "percent"
# Plausible usable capacity of one battery, used to tell 0-1 from 0-100.
CAPACITY_MIN_WH = 1_000.0
CAPACITY_MAX_WH = 60_000.0


def detect_soc_scale(soe: float, remaining_wh: float | None) -> str | None:
    """Tell whether ``stateOfEnergy`` is a 0-1 fraction or a 0-100 percentage.

    The docs say percentage, but the API has been seen returning 1.0 for a full
    battery. The two readings imply usable capacities 100x apart, so at most one
    is plausible. Returns None when the reading can't decide (e.g. empty battery).
    """
    if soe > 1.0:
        return SOC_PERCENT
    if soe <= 0 or not remaining_wh or remaining_wh <= 0:
        return None
    fraction_ok = CAPACITY_MIN_WH <= remaining_wh / soe <= CAPACITY_MAX_WH
    percent_ok = CAPACITY_MIN_WH <= remaining_wh / (soe / 100) <= CAPACITY_MAX_WH
    if fraction_ok != percent_ok:
        return SOC_FRACTION if fraction_ok else SOC_PERCENT
    return None


def battery_state(
    telemetry: DeviceTelemetry, scales: dict[str, str]
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Per-battery and combined state of charge, power, remaining energy and capacity.

    ``scales`` holds the detected SoC scale per battery and is updated in place.
    """
    per_battery: dict[str, dict[str, float]] = {}
    remaining_total = capacity_total = 0.0
    soc_values: list[float] = []
    for serial, metrics in telemetry.devices.items():

        def _last(metric: str, metrics: dict[str, Series] = metrics) -> float | None:
            series = metrics.get(metric)
            return series[-1][1] if series else None

        values: dict[str, float] = {}
        soe, remaining = _last("stateOfEnergy"), _last("remainingEnergy")
        charge, discharge = _last("chargePower"), _last("dischargePower")
        if remaining is not None:
            values["remaining_energy"] = remaining
            remaining_total += remaining
        if soe is not None:
            if serial not in scales and (detected := detect_soc_scale(soe, remaining)):
                scales[serial] = detected
            scale = scales.get(serial) or (SOC_FRACTION if soe <= 1.0 else SOC_PERCENT)
            percent = min(max(soe * 100 if scale == SOC_FRACTION else soe, 0.0), 100.0)
            values["state_of_charge"] = percent
            soc_values.append(percent)
            if remaining is not None and percent > 0.5:
                values["usable_capacity"] = remaining / (percent / 100)
                capacity_total += values["usable_capacity"]
        if charge is not None or discharge is not None:
            values["power"] = (discharge or 0.0) - (charge or 0.0)
        per_battery[serial] = values

    totals: dict[str, float] = {}
    if per_battery and any("remaining_energy" in v for v in per_battery.values()):
        totals["battery_remaining_energy"] = remaining_total
    if capacity_total:
        totals["battery_usable_capacity"] = capacity_total
        totals["battery_state_of_charge"] = min(remaining_total / capacity_total * 100, 100.0)
    elif soc_values:
        totals["battery_state_of_charge"] = sum(soc_values) / len(soc_values)
    return totals, per_battery


class SolarEdgeCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Polls SolarEdge only when the credit planner says a group is due."""

    config_entry: SolarEdgeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SolarEdgeConfigEntry,
        token_getter: Callable[[], Awaitable[str]],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=TICK_INTERVAL,
        )
        self.site_id: int = int(entry.data[CONF_SITE_ID])
        self.client = SolarEdgeClient(
            async_get_clientsession(hass), token_getter, on_request=self._on_request
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.ledger = CreditLedger()
        self.site: SiteInfo | None = None
        self.devices: list[Device] = []
        self.inventory_updated: datetime | None = None
        # None = unknown yet, True/False once a call has told us.
        self.capabilities: dict[str, bool | None] = {
            "meters": None,
            "storage": None,
            "site_power": False,
            "site_lifetime": False,
        }
        self.values: dict[str, float] = {}
        self.battery_values: dict[str, dict[str, float]] = {}
        self.soc_scales: dict[str, str] = {}
        # Last unit/value per battery metric, for diagnostics.
        self.raw_storage: dict[str, dict[str, Any]] = {}
        self.data_timestamp: datetime | None = None
        self.last_power: datetime | None = None
        self.last_energy: datetime | None = None
        self.counter_cursor: datetime | None = None
        self.counter_state: dict[str, dict[str, Any]] = {}
        self.poll_plan: PollPlan | None = None
        self._failures = 0
        self._retry_not_before: datetime | None = None

    # --- Options -------------------------------------------------------------

    @property
    def budget_config(self) -> BudgetConfig:
        """Budget options with defaults."""
        options = self.config_entry.options
        return BudgetConfig(
            monthly_budget=int(options.get(CONF_MONTHLY_BUDGET, DEFAULT_MONTHLY_BUDGET)),
            billing_day=int(options.get(CONF_BILLING_DAY, DEFAULT_BILLING_DAY)),
            day_share=float(options.get(CONF_DAY_SHARE, DEFAULT_DAY_SHARE)) / 100,
            energy_interval=timedelta(
                hours=float(options.get(CONF_ENERGY_INTERVAL, DEFAULT_ENERGY_INTERVAL))
            ),
        )

    @property
    def site_name(self) -> str:
        """Display name for the site."""
        return self.site.name if self.site else f"SolarEdge {self.site_id}"

    @property
    def has_battery(self) -> bool:
        """Whether the site has storage (inventory or telemetry says so)."""
        if self.capabilities["storage"] is not None:
            return bool(self.capabilities["storage"])
        return any(device.type == "BATTERY" for device in self.devices)

    @property
    def power_calls(self) -> int:
        """Credits one power refresh costs."""
        calls = 0 if self.capabilities["meters"] is False else 1
        calls += 1 if self.capabilities["site_power"] else 0
        calls += 1 if self.has_battery else 0
        return max(calls, 1)

    @property
    def energy_calls(self) -> int:
        """Credits one energy-counter refresh costs."""
        calls = 0 if self.capabilities["meters"] is False else 1
        calls += 1 if self.capabilities["site_lifetime"] else 0
        calls += 1 if self.has_battery else 0
        return max(calls, 1)

    @property
    def next_update(self) -> datetime | None:
        """When the next API call is planned."""
        if not self.poll_plan:
            return None
        candidates = [
            last + interval
            for last, interval in (
                (self.last_power, self.poll_plan.power_interval),
                (self.last_energy, self.poll_plan.energy_interval),
            )
            if last and interval
        ]
        if self._retry_not_before:
            candidates = [max(c, self._retry_not_before) for c in candidates]
        return min(candidates) if candidates else None

    @callback
    def _on_request(self) -> None:
        self.ledger.record()
        self._schedule_save()

    # --- Persistence ---------------------------------------------------------

    async def _async_setup(self) -> None:
        """Restore persisted state, then fetch inventory if it is stale."""
        if stored := await self._store.async_load():
            self._restore(stored)
        self.ledger.roll(dt_util.now(), self.budget_config.billing_day)
        self.config_entry.async_on_unload(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_save_now)
        )
        try:
            await self._async_refresh_inventory(force=self.site is None)
        except (SolarEdgeAuthError, OAuth2TokenRequestReauthError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (SolarEdgeError, OAuth2TokenRequestBaseError) as err:
            if self.site is None:
                raise UpdateFailed(f"Could not load site {self.site_id}: {err}") from err
            _LOGGER.warning("Inventory refresh failed, using cached copy: %s", err)
        self._register_devices()

    def _restore(self, stored: dict[str, Any]) -> None:
        def _dt(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        self.ledger = CreditLedger.from_dict(stored.get("ledger"))
        if site := stored.get("site"):
            self.site = SiteInfo(**site)
        self.devices = [Device(**device) for device in stored.get("devices", [])]
        self.inventory_updated = _dt(stored.get("inventory_updated"))
        self.capabilities.update(stored.get("capabilities", {}))
        self.values = dict(stored.get("values", {}))
        self.data_timestamp = _dt(stored.get("data_timestamp"))
        self.last_power = _dt(stored.get("last_power"))
        self.last_energy = _dt(stored.get("last_energy"))
        self.counter_cursor = _dt(stored.get("counter_cursor"))
        self.counter_state = stored.get("counter_state", {})
        self.battery_values = stored.get("battery_values", {})
        self.soc_scales = stored.get("soc_scales", {})

    def _data_to_save(self) -> dict[str, Any]:
        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "ledger": self.ledger.as_dict(),
            "site": asdict(self.site) if self.site else None,
            "devices": [asdict(device) for device in self.devices],
            "inventory_updated": _iso(self.inventory_updated),
            "capabilities": self.capabilities,
            "values": self.values,
            "data_timestamp": _iso(self.data_timestamp),
            "last_power": _iso(self.last_power),
            "last_energy": _iso(self.last_energy),
            "counter_cursor": _iso(self.counter_cursor),
            "counter_state": self.counter_state,
            "battery_values": self.battery_values,
            "soc_scales": self.soc_scales,
        }

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 10)

    async def _async_save_now(self, _event: Event | None = None) -> None:
        await self._store.async_save(self._data_to_save())

    async def async_shutdown(self) -> None:
        """Persist state on unload."""
        await super().async_shutdown()
        await self._async_save_now()

    # --- Inventory -----------------------------------------------------------

    async def _async_refresh_inventory(self, force: bool = False) -> None:
        now = dt_util.utcnow()
        if (
            not force
            and self.inventory_updated
            and now - self.inventory_updated < INVENTORY_REFRESH
        ):
            return
        self.site = await self.client.get_site(self.site_id)
        try:
            self.devices = await self.client.get_devices(self.site_id)
        except SolarEdgeForbiddenError:
            _LOGGER.warning("No DEVICE_DATA access; device inventory unavailable")
        else:
            self._update_meter_capability()
        self.inventory_updated = now
        self._schedule_save()

    def _update_meter_capability(self) -> None:
        """Skip the meter calls while the inventory has no active meter.

        Sites can list meters that never report (``active: false``); their telemetry
        and counters come back empty but each call still costs a credit. Re-checked
        with every inventory refresh, so a meter that starts reporting is picked up.
        """
        if any(d.type == "METER" and d.active is not False for d in self.devices):
            if self.capabilities["meters"] is False:
                self.capabilities["meters"] = None
        else:
            self.capabilities["meters"] = False

    @callback
    def _register_devices(self) -> None:
        registry = dr.async_get(self.hass)
        site_identifier = (DOMAIN, str(self.site_id))
        registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            identifiers={site_identifier},
            name=self.site_name,
            manufacturer="SolarEdge",
            model="Monitoring site",
            configuration_url=MONITORING_URL,
        )
        # Battery devices are created by their entities. Anything else (inverters,
        # meters, retired batteries) has no entities and would sit empty.
        keep = {site_identifier} | {(DOMAIN, serial) for serial in self.battery_serials}
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            if not device.identifiers & keep:
                registry.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )

    @property
    def battery_serials(self) -> list[str]:
        """Batteries that report telemetry (or, before the first poll, the inventory)."""
        if self.battery_values:
            return list(self.battery_values)
        return [device.serial_number for device in self.devices if device.type == "BATTERY"]

    def battery_device(self, serial: str) -> Device | None:
        """Inventory record for a battery serial."""
        return next((d for d in self.devices if d.serial_number == serial), None)

    # --- Planning ------------------------------------------------------------

    def _day_hours(self) -> float:
        today = dt_util.now().date()
        sunrise = get_astral_event_date(self.hass, "sunrise", today)
        sunset = get_astral_event_date(self.hass, "sunset", today)
        if sunrise and sunset and sunset > sunrise:
            return (sunset - sunrise).total_seconds() / 3600
        return 12.0

    def _make_plan(self, now: datetime) -> PollPlan:
        return plan(
            now,
            self.budget_config,
            self.ledger,
            power_calls=self.power_calls,
            energy_calls=self.energy_calls,
            is_day=is_up(self.hass),
            day_hours=self._day_hours(),
        )

    # --- Update --------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, float]:
        now = dt_util.now()
        config = self.budget_config
        if self.ledger.roll(now, config.billing_day):
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            self._schedule_save()
        self.poll_plan = self._make_plan(now)

        if self._retry_not_before and now < self._retry_not_before:
            return dict(self.values)

        try:
            await self._async_refresh_inventory()
            if is_due(self.last_power, self.poll_plan.power_interval, now):
                await self._async_fetch_power(now)
                self.last_power = now
            if is_due(self.last_energy, self.poll_plan.energy_interval, now):
                await self._async_fetch_energy(now)
                self.last_energy = now
        except (SolarEdgeAuthError, OAuth2TokenRequestReauthError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolarEdgeCreditLimitError:
            self._handle_credits_exhausted()
        except SolarEdgeRateLimitError as err:
            raise UpdateFailed(
                f"SolarEdge rate limit hit: {err}", retry_after=err.retry_after
            ) from err
        except (SolarEdgeError, OAuth2TokenRequestBaseError) as err:
            self._failures += 1
            backoff = min(FAILURE_BACKOFF_MIN * 2 ** (self._failures - 1), FAILURE_BACKOFF_MAX)
            self._retry_not_before = now + backoff
            raise UpdateFailed(f"Error talking to SolarEdge: {err}") from err
        else:
            self._failures = 0
            self._retry_not_before = None
        finally:
            self._schedule_save()
        return dict(self.values)

    @property
    def _issue_id(self) -> str:
        return f"credits_exhausted_{self.config_entry.entry_id}"

    @callback
    def _handle_credits_exhausted(self) -> None:
        self.ledger.exhausted = True
        cycle_end = self.poll_plan.cycle_end if self.poll_plan else None
        _LOGGER.warning("SolarEdge monthly credits exhausted; polling paused until %s", cycle_end)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="credits_exhausted",
            translation_placeholders={
                "site": self.site_name,
                "reset": cycle_end.date().isoformat() if cycle_end else "the next cycle",
            },
        )

    def _set_timestamp(self, ts: datetime | None) -> None:
        if ts and (self.data_timestamp is None or ts > self.data_timestamp):
            self.data_timestamp = ts

    async def _async_fetch_power(self, now: datetime) -> None:
        start = now - TELEMETRY_LOOKBACK
        values: dict[str, float] = {}

        if self.capabilities["meters"] is not False:
            try:
                meters = await self.client.get_meter_telemetry(self.site_id, start, now)
            except SolarEdgeForbiddenError:
                _LOGGER.warning("Meter telemetry not permitted; falling back to site power")
                self.capabilities["meters"] = False
            else:
                if meters.devices:
                    self.capabilities["meters"] = True
                for metric, key in METER_POWER_KEYS.items():
                    if latest := latest_total(meters, metric):
                        values[key] = latest[1]
                        self._set_timestamp(latest[0])

        self.capabilities["site_power"] = "solar_power" not in values
        if self.capabilities["site_power"]:
            series: Series = await self.client.get_site_power(self.site_id, start, now)
            if series:
                values["solar_power"] = series[-1][1]
                self._set_timestamp(series[-1][0])

        if self.has_battery:
            try:
                storage = await self.client.get_storage_telemetry(self.site_id, start, now)
            except SolarEdgeForbiddenError:
                _LOGGER.warning("Storage telemetry not permitted; battery sensors disabled")
                self.capabilities["storage"] = False
            else:
                self.capabilities["storage"] = bool(storage.devices)
                for metric, key in (
                    ("chargePower", "battery_charge_power"),
                    ("dischargePower", "battery_discharge_power"),
                ):
                    if latest := latest_total(storage, metric):
                        values[key] = latest[1]
                        self._set_timestamp(latest[0])
                totals, per_battery = battery_state(storage, self.soc_scales)
                values.update(totals)
                self.battery_values.update(per_battery)
                self.raw_storage = {
                    serial: {
                        metric: {
                            "unit": storage.units.get(serial, {}).get(metric),
                            "last": series[-1][1] if series else None,
                        }
                        for metric, series in metrics.items()
                    }
                    for serial, metrics in storage.devices.items()
                }

        if "grid_import_power" in values or "grid_export_power" in values:
            values["grid_power"] = values.get("grid_import_power", 0.0) - values.get(
                "grid_export_power", 0.0
            )
        if "battery_charge_power" in values or "battery_discharge_power" in values:
            values["battery_power"] = values.get("battery_discharge_power", 0.0) - values.get(
                "battery_charge_power", 0.0
            )
        self.values.update(values)

    async def _async_fetch_energy(self, now: datetime) -> None:
        start = (
            self.counter_cursor - timedelta(hours=1)
            if self.counter_cursor
            else now - COUNTER_BACKFILL
        )
        start = max(start, now - COUNTER_MAX_WINDOW)
        counters: dict[str, dict[str, Series]] = {}

        def _collect(readings: CounterReadings, mapping: dict[str, str]) -> None:
            for direction, key in mapping.items():
                if direction in readings.directions:
                    counters[key] = readings.directions[direction]

        if self.capabilities["meters"] is not False:
            try:
                _collect(
                    await self.client.get_meter_lifetime_energy(self.site_id, start, now),
                    METER_COUNTER_KEYS,
                )
            except SolarEdgeForbiddenError:
                _LOGGER.warning("Meter counters not permitted; using site lifetime energy")
                self.capabilities["meters"] = False

        self.capabilities["site_lifetime"] = "solar" not in counters
        if self.capabilities["site_lifetime"]:
            _, energy = await self.client.get_site_lifetime_energy(self.site_id)
            if energy is not None:
                self.values["solar_energy"] = energy

        if self.has_battery:
            try:
                _collect(
                    await self.client.get_storage_lifetime_energy(self.site_id, start, now),
                    STORAGE_COUNTER_KEYS,
                )
            except SolarEdgeForbiddenError:
                _LOGGER.warning("Storage counters not permitted")

        latest_per_key: list[datetime] = []
        for key, series_by_device in counters.items():
            last_points = [series[-1] for series in series_by_device.values() if series]
            if not last_points:
                continue
            self.values[f"{key}_energy"] = sum(value for _, value in last_points)
            latest_per_key.append(min(ts for ts, _ in last_points))
        if latest_per_key:
            self.counter_cursor = min(latest_per_key)

        # The site counter has no history window, so only per-device counters
        # become hourly statistics.
        async_import_counters(self.hass, self.site_id, self.site_name, counters, self.counter_state)
