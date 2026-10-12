"""Tests for the SolarEdge API client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.solaredge_v2.api import (
    SolarEdgeAuthError,
    SolarEdgeClient,
    SolarEdgeCreditLimitError,
    SolarEdgeForbiddenError,
    SolarEdgeRateLimitError,
    format_datetime,
    scale_to_base,
)

from .conftest import SITE_ID, SITE_URL, load_json

START = datetime(2026, 10, 12, 9, tzinfo=UTC)
END = START + timedelta(hours=3)


def _client(hass: HomeAssistant, calls: list[int] | None = None) -> SolarEdgeClient:
    async def _token() -> str:
        return "token"

    return SolarEdgeClient(
        async_get_clientsession(hass),
        _token,
        on_request=(lambda: calls.append(1)) if calls is not None else None,
    )


def test_format_datetime() -> None:
    """Timestamps are sent in UTC with a zone; naive values are refused."""
    local = datetime(2026, 10, 12, 9, 30, 15, 123, tzinfo=timezone(timedelta(hours=-5)))
    assert format_datetime(local) == "2026-10-12T14:30:15Z"
    with pytest.raises(ValueError):
        format_datetime(datetime(2026, 10, 12))


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [(1.5, "KW", 1500), (2, "MWH", 2_000_000), (7, "W", 7), (7, "WH", 7), (7, None, 7)],
)
def test_scale_to_base(value: float, unit: str | None, expected: float) -> None:
    """Units are normalized to W / Wh."""
    assert scale_to_base(value, unit) == expected


async def test_meter_telemetry_parsing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Nulls are dropped, kW converted, and the bearer header and zone-aware range sent."""
    aioclient_mock.get(f"{SITE_URL}/meters/telemetry", json=load_json("meters_telemetry.json"))
    calls: list[int] = []
    telemetry = await _client(hass, calls).get_meter_telemetry(SITE_ID, START, END)

    grid = telemetry.devices["2644513869"]
    assert [v for _, v in grid["importPower"]] == [0.0]
    assert grid["consumptionPower"][0][1] == 1800.0
    assert telemetry.devices["617887617"]["productionPower"][-1][1] == 5200.0
    assert calls == [1]

    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == "Bearer token"
    assert url.query["from"] == "2026-10-12T09:00:00Z"
    assert url.query["to"] == "2026-10-12T12:00:00Z"


async def test_lifetime_counters(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Empty directions are omitted and readings keep their timestamps."""
    aioclient_mock.get(f"{SITE_URL}/meters/lifetime-energy", json=load_json("meters_lifetime.json"))
    readings = await _client(hass).get_meter_lifetime_energy(SITE_ID, START, END)
    assert set(readings.directions) == {"importEnergy", "exportEnergy", "productionEnergy"}
    assert readings.directions["productionEnergy"]["617887617"][-1][1] == 6203300


async def test_devices_request_all_types(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Inventory asks for inverters, meters and batteries and reads the parent."""
    aioclient_mock.get(f"{SITE_URL}/devices", json=load_json("devices.json"))
    devices = await _client(hass).get_devices(SITE_ID)
    assert [d.type for d in devices] == ["INVERTER", "METER", "BATTERY"]
    assert devices[1].parent_serial == "7E1234AB-CD"
    assert devices[0].firmware.startswith("4.25")
    _, url, _, _ = aioclient_mock.mock_calls[0]
    assert url.query.getall("types") == ["INVERTER", "METER", "BATTERY"]


@pytest.mark.parametrize(
    ("status", "headers", "body", "error"),
    [
        (401, {}, '{"message": "Unauthorized"}', SolarEdgeAuthError),
        (403, {}, '{"title": "Forbidden"}', SolarEdgeForbiddenError),
        (
            429,
            {"x-ratelimit-remaining-minute": "0", "retry-after": "42"},
            '{"message": "API rate limit exceeded"}',
            SolarEdgeRateLimitError,
        ),
        (
            429,
            {"x-ratelimit-remaining-minute": "7"},
            '{"detail": "Monthly credit limit reached."}',
            SolarEdgeCreditLimitError,
        ),
    ],
)
async def test_error_mapping(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    headers: dict[str, str],
    body: str,
    error: type[Exception],
) -> None:
    """HTTP errors map to typed exceptions, and failed calls still count a credit."""
    aioclient_mock.get(SITE_URL, status=status, text=body, headers=headers)
    calls: list[int] = []
    with pytest.raises(error) as exc_info:
        await _client(hass, calls).get_site(SITE_ID)
    assert calls == [1]
    if isinstance(exc_info.value, SolarEdgeRateLimitError):
        assert exc_info.value.retry_after == 42
