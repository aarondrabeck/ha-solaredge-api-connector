"""Integration tests: setup, sensors, credit accounting and statistics."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
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
    assert float(_state(hass, "sensor.home_battery_state_of_charge")) == pytest.approx(50)
    assert float(_state(hass, "sensor.home_battery_usable_capacity")) == pytest.approx(9.7)
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
