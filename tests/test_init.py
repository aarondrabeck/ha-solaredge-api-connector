"""Integration tests: setup, sensors, credit accounting and statistics."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.solaredge_v2.const import DOMAIN

from .conftest import SITE_URL

NOW = "2026-10-12T17:00:00+00:00"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_setup_and_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """First refresh fills every sensor using six credits."""
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert float(_state(hass, "sensor.home_solar_power")) == pytest.approx(5.2)
    assert float(_state(hass, "sensor.home_home_power")) == pytest.approx(1.8)
    # Exporting: HA polarity is positive = import.
    assert float(_state(hass, "sensor.home_grid_power")) == pytest.approx(-1.2005)
    assert _state(hass, "sensor.home_grid_status") == "exporting"
    # Charging: positive = discharge.
    assert float(_state(hass, "sensor.home_battery_power")) == pytest.approx(-2.0)
    assert _state(hass, "sensor.home_battery_status") == "charging"
    # The API reports state of energy as a 0-1 fraction (1.0 = full).
    assert float(_state(hass, "sensor.home_battery_state_of_charge")) == pytest.approx(100)
    assert float(_state(hass, "sensor.home_battery_usable_capacity")) == pytest.approx(4.85)
    # Per-battery entities live on the battery's own device.
    assert float(_state(hass, "sensor.battery_1_state_of_charge")) == pytest.approx(100)
    assert float(_state(hass, "sensor.battery_1_power")) == pytest.approx(-2.0)
    assert float(_state(hass, "sensor.battery_1_remaining_energy")) == pytest.approx(4.85)
    assert float(_state(hass, "sensor.home_grid_import_energy")) == pytest.approx(4489.617)
    assert float(_state(hass, "sensor.home_solar_energy")) == pytest.approx(6203.3)
    assert float(_state(hass, "sensor.home_battery_charge_energy")) == pytest.approx(3000000)

    # site + devices + 2 telemetry + 2 counters
    assert _state(hass, "sensor.home_api_credits_used") == "6"
    assert int(_state(hass, "sensor.home_api_credits_projected")) <= 1800

    # A tick before anything is due must not spend credits.
    calls = mock_api.call_count
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert mock_api.call_count == calls
    assert _state(hass, "sensor.home_api_credits_used") == "6"

    # The complete 15:00 UTC hour was imported as an external statistic.
    await async_wait_recording_done(hass)
    stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, f"{DOMAIN}:3581845_grid_import", True, {"sum"}
    )
    row = stats[f"{DOMAIN}:3581845_grid_import"][0]
    assert row["sum"] == pytest.approx(4489.6)


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_credit_exhaustion_raises_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A credit-limit 429 pauses polling and creates a repair issue."""
    from .conftest import load_json

    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    aioclient_mock.get(f"{SITE_URL}/devices", json=load_json("devices.json"))
    aioclient_mock.get(
        f"{SITE_URL}/meters/telemetry",
        status=429,
        text='{"detail": "Monthly credit limit reached."}',
        headers={"x-ratelimit-remaining-minute": "5"},
    )
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"credits_exhausted_{config_entry.entry_id}")
    assert issue is not None
    assert config_entry.runtime_data.ledger.exhausted


@pytest.mark.usefixtures("setup_credentials")
async def test_auth_error_starts_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A rejected token puts the entry into reauth."""
    aioclient_mock.get(SITE_URL, status=401, text='{"message": "Unauthorized"}')
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_restart_reuses_cached_state(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """Reloading restores from storage without spending credits."""
    await _setup(hass, config_entry)
    calls = mock_api.call_count
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert mock_api.call_count == calls
    assert float(_state(hass, "sensor.home_solar_power")) == pytest.approx(5.2)


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_only_site_and_battery_devices(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """Inverters and meters don't become empty devices; stale ones are removed."""
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, "7E1234AB-CD")}
    )
    await _setup(hass, config_entry)
    identifiers = {
        next(iter(device.identifiers))[1]
        for device in dr.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    assert identifiers == {"3581845", "BATT-001"}
    battery = next(
        d
        for d in dr.async_entries_for_config_entry(registry, config_entry.entry_id)
        if (DOMAIN, "BATT-001") in d.identifiers
    )
    assert battery.model == "Home Battery 10kWh"
    assert battery.via_device_id is not None


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_inactive_meters_are_not_polled(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Meters the inventory marks inactive cost no credits; site endpoints are used."""
    from .conftest import load_json

    devices = [d for d in load_json("devices.json") if d["type"] != "BATTERY"]
    for device in devices:
        if device["type"] == "METER":
            device["active"] = False
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    aioclient_mock.get(f"{SITE_URL}/devices", json=devices)
    aioclient_mock.get(
        f"{SITE_URL}/power",
        json={"values": [{"timestamp": "2026-10-12T16:45:00Z", "value": 4100.0}]},
    )
    aioclient_mock.get(
        f"{SITE_URL}/lifetime-energy",
        json={"energy": 6203300.0, "unit": "WH", "lastUpdateTime": "2026-10-12T16:45:00Z"},
    )
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert not any("/meters/" in str(call[1]) for call in aioclient_mock.mock_calls)
    assert float(_state(hass, "sensor.home_solar_power")) == pytest.approx(4.1)
    # site + devices + site power + site lifetime energy
    assert _state(hass, "sensor.home_api_credits_used") == "4"
    coordinator = config_entry.runtime_data
    assert coordinator.capabilities["meters"] is False
    assert coordinator.power_calls == 1
    assert coordinator.energy_calls == 1

    # A meter that starts reporting is probed again after the next inventory refresh.
    next(d for d in coordinator.devices if d.type == "METER").active = True
    coordinator._update_meter_capability()
    assert coordinator.capabilities["meters"] is None
