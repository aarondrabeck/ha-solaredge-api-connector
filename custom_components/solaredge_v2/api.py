"""Async client for the SolarEdge Monitoring API v2 (Basic Monitoring).

Every call costs one credit, so the client reports each request it sends
through ``on_request`` and keeps a small calls-per-minute limiter.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import time
from typing import Any

import aiohttp

from .const import API_BASE_URL, LIFETIME_START, MAX_CALLS_PER_MINUTE

_LOGGER = logging.getLogger(__name__)

# A retry delay longer than this means the monthly allowance is gone, not the per-minute one.
MAX_RATE_LIMIT_RETRY_SECONDS = 3600

type Series = list[tuple[datetime, float]]
type DeviceStatus = dict[str, dict[str, float | str]]


class SolarEdgeError(Exception):
    """Base error for the SolarEdge API."""


class SolarEdgeConnectionError(SolarEdgeError):
    """The API could not be reached."""


class SolarEdgeAuthError(SolarEdgeError):
    """The token was rejected (401)."""


class SolarEdgeForbiddenError(SolarEdgeError):
    """No access to the site, missing scope or tier too low (403)."""


class SolarEdgeNotFoundError(SolarEdgeError):
    """The site or device does not exist (404)."""


class SolarEdgeBadRequestError(SolarEdgeError):
    """The API refused the request, e.g. a window that is too long (400)."""


class SolarEdgeRateLimitError(SolarEdgeError):
    """Calls-per-minute limit hit (429 with remaining-minute 0)."""

    def __init__(self, retry_after: float | None) -> None:
        """Store the retry delay in seconds."""
        super().__init__(f"Rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class SolarEdgeCreditLimitError(SolarEdgeError):
    """Monthly credit allowance exhausted (429 'Monthly credit limit reached.')."""


def format_datetime(value: datetime) -> str:
    """Format a timestamp the API accepts; it rejects values without a zone."""
    if value.tzinfo is None:
        raise ValueError("SolarEdge API timestamps must be timezone aware")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    """Parse an API timestamp (always carries an offset)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_series(values: list[dict[str, Any]] | None) -> Series:
    """Parse ``[{timestamp, value}]`` dropping null values (null = no data)."""
    series: Series = []
    for item in values or []:
        if item.get("value") is None or not item.get("timestamp"):
            continue
        series.append((parse_datetime(item["timestamp"]), float(item["value"])))
    series.sort(key=lambda point: point[0])
    return series


def scale_to_base(value: float, unit: str | None) -> float:
    """Convert W/kW/MW and WH/KWH/MWH values to W or Wh."""
    factor = {"K": 1_000.0, "M": 1_000_000.0}.get((unit or "")[:1].upper(), 1.0)
    return value * factor


@dataclass(slots=True)
class SiteInfo:
    """Site metadata."""

    site_id: int
    name: str
    peak_power: float | None = None
    timezone: str | None = None
    installation_date: str | None = None


@dataclass(slots=True)
class Device:
    """A device from the site inventory."""

    serial_number: str
    type: str
    name: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    parent_serial: str | None = None
    meter_type: str | None = None
    # False once the device was removed or replaced.
    active: bool = True
    # What an environmental sensor measures, e.g. PLANE_OF_ARRAY.
    measurement: str | None = None


@dataclass(slots=True)
class DeviceTelemetry:
    """Bulk telemetry: ``{serial: {metric: series}}`` with values in W/Wh/%."""

    devices: dict[str, dict[str, Series]] = field(default_factory=dict)
    units: dict[str, dict[str, str | None]] = field(default_factory=dict)


@dataclass(slots=True)
class CounterReadings:
    """Lifetime counter readings: ``{direction: {serial: series}}`` in Wh."""

    directions: dict[str, dict[str, Series]] = field(default_factory=dict)


class SolarEdgeClient:
    """Minimal client for the endpoints this integration uses."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_getter: Callable[[], Awaitable[str]],
        on_request: Callable[[], None] | None = None,
        base_url: str = API_BASE_URL,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._token_getter = token_getter
        self._on_request = on_request
        self._base_url = base_url
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        """Keep below the per-minute ceiling (shared by all apps on the account)."""
        async with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) >= MAX_CALLS_PER_MINUTE:
                await asyncio.sleep(60 - (now - self._calls[0]))
                self._calls.popleft()
            self._calls.append(time.monotonic())

    async def _get(self, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        token = await self._token_getter()
        await self._throttle()
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                # The request reached the gateway, so assume it was billed.
                if self._on_request:
                    self._on_request()
                if resp.status == 200:
                    return await resp.json(content_type=None)
                body = await resp.text()
                await self._raise_for_status(resp, body)
        except TimeoutError as err:
            raise SolarEdgeConnectionError(f"Timeout calling {path}") from err
        except aiohttp.ClientError as err:
            raise SolarEdgeConnectionError(f"Error calling {path}: {err}") from err
        raise SolarEdgeError(f"Unexpected response from {path}")  # pragma: no cover

    @staticmethod
    async def _raise_for_status(resp: aiohttp.ClientResponse, body: str) -> None:
        status = resp.status
        _LOGGER.debug("SolarEdge %s %s: %s", status, resp.url.path, body[:300])
        if status == 400:
            raise SolarEdgeBadRequestError(body[:200])
        if status == 401:
            raise SolarEdgeAuthError(body[:200])
        if status == 403:
            raise SolarEdgeForbiddenError(body[:200])
        if status == 404:
            raise SolarEdgeNotFoundError(body[:200])
        if status == 429:
            remaining_minute = resp.headers.get("x-ratelimit-remaining-minute")
            retry_after = resp.headers.get("retry-after")
            delay = float(retry_after) if retry_after else None
            if (
                "credit limit" in body.lower()
                or resp.headers.get("x-ratelimit-remaining-month") == "0"
                or (delay is not None and delay > MAX_RATE_LIMIT_RETRY_SECONDS)
                or (remaining_minute not in (None, "0") and delay is None)
            ):
                raise SolarEdgeCreditLimitError(body[:200])
            raise SolarEdgeRateLimitError(delay)
        raise SolarEdgeError(f"HTTP {status}: {body[:200]}")

    # --- Sites -----------------------------------------------------------------

    async def get_site(self, site_id: int) -> SiteInfo:
        """Return site metadata (also used to validate access to a site)."""
        data = await self._get(f"/sites/{site_id}")
        location = data.get("location") or {}
        return SiteInfo(
            site_id=int(data.get("siteId", site_id)),
            name=data.get("name") or f"SolarEdge {site_id}",
            peak_power=data.get("peakPower"),
            timezone=location.get("timezone"),
            installation_date=data.get("installationDate"),
        )

    async def get_devices(self, site_id: int) -> list[Device]:
        """Return the inverters, meters, batteries, EV chargers and sensors at the site."""
        params = [("types", t) for t in ("INVERTER", "METER", "BATTERY", "EV_CHARGER", "SENSOR")]
        data = await self._get(f"/sites/{site_id}/devices", params)
        devices: list[Device] = []
        for item in data or []:
            # Environmental sensors have an id instead of a serial number.
            serial = item.get("serialNumber") or item.get("id")
            if not serial:
                continue
            parent = item.get("parent") or {}
            devices.append(
                Device(
                    serial_number=serial,
                    type=item.get("type") or "UNKNOWN",
                    name=item.get("name"),
                    manufacturer=item.get("manufacturer"),
                    model=item.get("model"),
                    firmware=item.get("firmware") or item.get("firmwareVersion"),
                    parent_serial=parent.get("serialNumber") or item.get("connectedTo"),
                    meter_type=item.get("meterType"),
                    active=item.get("active") is not False,
                    measurement=item.get("measurement"),
                )
            )
        return devices

    async def get_site_power(self, site_id: int, start: datetime, end: datetime) -> Series:
        """Return site PV production power (W) at quarter-hour resolution."""
        data = await self._get(
            f"/sites/{site_id}/power",
            [
                ("from", format_datetime(start)),
                ("to", format_datetime(end)),
                ("resolution", "QUARTER_HOUR"),
                ("unit", "W"),
            ],
        )
        return parse_series(data.get("values"))

    # --- Bulk telemetry --------------------------------------------------------

    async def get_meter_telemetry(
        self, site_id: int, start: datetime, end: datetime
    ) -> DeviceTelemetry:
        """Return power/energy telemetry for all meters."""
        data = await self._get(
            f"/sites/{site_id}/meters/telemetry",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        return self._parse_telemetry(data.get("meters"))

    async def get_storage_telemetry(
        self, site_id: int, start: datetime, end: datetime
    ) -> DeviceTelemetry:
        """Return power/energy/state-of-energy telemetry for all batteries."""
        data = await self._get(
            f"/sites/{site_id}/storage/telemetry",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        return self._parse_telemetry(data.get("storage"))

    async def get_ev_charger_telemetry(
        self, site_id: int, start: datetime, end: datetime
    ) -> DeviceTelemetry:
        """Return charging power and energy for all EV chargers."""
        data = await self._get(
            f"/sites/{site_id}/ev-chargers/telemetry",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        return self._parse_telemetry(data.get("evChargers"))

    async def get_ev_charger_lifetime_energy(self, site_id: int, end: datetime) -> dict[str, float]:
        """Return the energy each EV charger has delivered so far (Wh).

        Chargers have no onboard counter; TOTAL sums their whole history instead.
        """
        data = await self._get(
            f"/sites/{site_id}/ev-chargers/telemetry",
            [
                ("from", format_datetime(LIFETIME_START)),
                ("to", format_datetime(end)),
                ("resolution", "TOTAL"),
            ],
        )
        totals: dict[str, float] = {}
        for serial, metrics in (data.get("evChargers") or {}).items():
            energy = (metrics or {}).get("chargingEnergy") or {}
            values = [i["value"] for i in energy.get("values") or [] if i.get("value") is not None]
            if values:
                totals[serial] = scale_to_base(float(values[-1]), energy.get("unit"))
        return totals

    async def get_sensor_telemetry(
        self, site_id: int, start: datetime, end: datetime
    ) -> dict[str, float]:
        """Return the latest reading of each environmental sensor, keyed by sensor id."""
        data = await self._get(
            f"/sites/{site_id}/sensors/telemetry",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        readings: dict[str, float] = {}
        for sensor_id, sensor in (data.get("sensors") or {}).items():
            if series := parse_series((sensor or {}).get("values")):
                readings[sensor_id] = series[-1][1]
        return readings

    @staticmethod
    def _parse_telemetry(raw: dict[str, Any] | None) -> DeviceTelemetry:
        telemetry = DeviceTelemetry()
        for serial, metrics in (raw or {}).items():
            parsed: dict[str, Series] = {}
            units: dict[str, str | None] = {}
            for metric, payload in (metrics or {}).items():
                if not isinstance(payload, dict):
                    continue
                unit = payload.get("unit")
                units[metric] = unit
                series = parse_series(payload.get("values"))
                if unit and unit.upper() != "PERCENTAGE":
                    series = [(ts, scale_to_base(v, unit)) for ts, v in series]
                parsed[metric] = series
            telemetry.devices[serial] = parsed
            telemetry.units[serial] = units
        return telemetry

    # --- Device status ---------------------------------------------------------

    async def get_inverter_status(
        self, site_id: int, start: datetime, end: datetime
    ) -> DeviceStatus:
        """Return the last known mode, temperature and power limit of each inverter."""
        data = await self._get(
            f"/sites/{site_id}/inverters/status",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        return self._latest_status(data.get("inverters"))

    async def get_storage_status(
        self, site_id: int, start: datetime, end: datetime
    ) -> DeviceStatus:
        """Return the last known state code and internal temperature of each battery."""
        data = await self._get(
            f"/sites/{site_id}/storage/status",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        return self._latest_status(data.get("storage"))

    @staticmethod
    def _latest_status(raw: dict[str, Any] | None) -> DeviceStatus:
        """Keep the newest sample per metric; unlike telemetry, values can be strings."""
        status: DeviceStatus = {}
        for serial, metrics in (raw or {}).items():
            latest: dict[str, float | str] = {}
            for metric, payload in (metrics or {}).items():
                if not isinstance(payload, dict):
                    continue
                samples = payload.get("values") or []
                values = [i["value"] for i in samples if i.get("value") is not None]
                if values:
                    latest[metric] = values[-1]
            status[serial] = latest
        return status

    # --- Lifetime counters -----------------------------------------------------

    async def get_inverter_lifetime_energy(
        self, site_id: int, start: datetime, end: datetime
    ) -> CounterReadings:
        """Return onboard inverter production counters."""
        data = await self._get(
            f"/sites/{site_id}/inverters/lifetime-energy",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        readings = CounterReadings()
        for inverter in data.get("inverters") or []:
            serial = inverter.get("serialNumber") or "inverter"
            series = parse_series(inverter.get("lifetimeEnergy"))
            if series:
                unit = inverter.get("unit")
                readings.directions.setdefault("lifetimeEnergy", {})[serial] = [
                    (ts, scale_to_base(v, unit)) for ts, v in series
                ]
        return readings

    async def get_meter_lifetime_energy(
        self, site_id: int, start: datetime, end: datetime
    ) -> CounterReadings:
        """Return onboard meter counters (import/export/production/consumption)."""
        data = await self._get(
            f"/sites/{site_id}/meters/lifetime-energy",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        readings = CounterReadings()
        for meter in data.get("meters") or []:
            serial = meter.get("serialNumber") or "meter"
            unit = meter.get("unit")
            for direction in (
                "productionEnergy",
                "consumptionEnergy",
                "importEnergy",
                "exportEnergy",
            ):
                series = parse_series(meter.get(direction))
                if series:
                    readings.directions.setdefault(direction, {})[serial] = [
                        (ts, scale_to_base(v, unit)) for ts, v in series
                    ]
        return readings

    async def get_storage_lifetime_energy(
        self, site_id: int, start: datetime, end: datetime
    ) -> CounterReadings:
        """Return onboard battery charge/discharge counters."""
        data = await self._get(
            f"/sites/{site_id}/storage/lifetime-energy",
            [("from", format_datetime(start)), ("to", format_datetime(end))],
        )
        readings = CounterReadings()
        for battery in data.get("batteries") or []:
            serial = battery.get("serialNumber") or "battery"
            unit = battery.get("unit")
            for direction in ("lifetimeEnergyCharged", "lifetimeEnergyDischarged"):
                series = parse_series(battery.get(direction))
                if series:
                    readings.directions.setdefault(direction, {})[serial] = [
                        (ts, scale_to_base(v, unit)) for ts, v in series
                    ]
        return readings
