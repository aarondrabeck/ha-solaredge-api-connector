"""Integration tests: setup, sensors, credit accounting and statistics."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.solaredge_v2.const import (
    CONF_STATUS_INTERVAL,
    DOMAIN,
    MAX_QUERY_WINDOW,
)
from custom_components.solaredge_v2.diagnostics import async_get_config_entry_diagnostics

from .conftest import SITE_ID, SITE_URL, load_json

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
    """First refresh fills every sensor using eight credits."""
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

    # Device status arrives on the inverter's own device and the battery's.
    assert _state(hass, "sensor.inverter_1_mode") == "MPPT"
    assert float(_state(hass, "sensor.inverter_1_temperature")) == pytest.approx(37.5)
    assert float(_state(hass, "sensor.battery_1_internal_temperature")) == pytest.approx(28.3)

    # site + devices + 2 telemetry + 2 counters + 2 status
    assert _state(hass, "sensor.home_api_credits_used") == "8"
    assert int(_state(hass, "sensor.home_api_credits_projected")) <= 1800

    # A tick before anything is due must not spend credits.
    calls = mock_api.call_count
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert mock_api.call_count == calls
    assert _state(hass, "sensor.home_api_credits_used") == "8"

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
async def test_only_devices_with_entities(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """Meters don't become empty devices; stale ones are removed."""
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, "2644513869")}
    )
    await _setup(hass, config_entry)
    # Inverters only become devices once status reports them, so a reload picks this one up.
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    identifiers = {
        next(iter(device.identifiers))[1]
        for device in dr.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    assert identifiers == {"3581845", "BATT-001", "7E1234AB-CD"}
    battery = next(
        d
        for d in dr.async_entries_for_config_entry(registry, config_entry.entry_id)
        if (DOMAIN, "BATT-001") in d.identifiers
    )
    assert battery.model == "Home Battery 10kWh"
    assert battery.via_device_id is not None


def _windows(mock: AiohttpClientMocker, path: str) -> list[tuple[datetime, datetime]]:
    """Return the from/to of every request to ``path``."""
    return [
        (datetime.fromisoformat(url.query["from"]), datetime.fromisoformat(url.query["to"]))
        for _, url, _, _ in mock.mock_calls
        if url.path.endswith(path)
    ]


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_counter_requests_stay_within_the_api_window(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """The API rejects counter windows over 12 hours, including on the very first fetch."""
    await _setup(hass, config_entry)
    windows = _windows(mock_api, "/lifetime-energy")
    assert len(windows) == 2
    assert all(end - start <= timedelta(hours=12) for start, end in windows)


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_long_gap_is_walked_one_window_at_a_time(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After an outage every hour is still fetched, without exceeding the window."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    start = dt_util.now()
    coordinator.counter_cursor = start - timedelta(hours=30)
    mock_api.clear_requests()
    empty = {"meters": [], "batteries": [], "inverters": []}
    for path in ("meters", "storage", "inverters"):
        mock_api.get(f"{SITE_URL}/{path}/lifetime-energy", json=empty)

    # One window per coordinator tick, as in production.
    steps = 1
    while not await coordinator._async_fetch_energy(dt_util.now()):
        steps += 1
        assert steps < 10, "the walk never finished"
        freezer.tick(timedelta(minutes=5))

    windows = _windows(mock_api, "/meters/lifetime-energy")
    assert len(windows) == steps == 3
    assert all(end - start <= MAX_QUERY_WINDOW for start, end in windows)
    assert windows[0][0] == start - timedelta(hours=31)
    assert windows[-1][1] == dt_util.now()
    # Consecutive windows overlap, so no hour falls between two of them.
    assert all(later[0] < earlier[1] for earlier, later in pairwise(windows))


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_very_old_gap_is_not_walked_forever(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A month offline must not spend a month of windows."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    coordinator.counter_cursor = dt_util.now() - timedelta(days=30)
    mock_api.clear_requests()
    empty = {"meters": [], "batteries": [], "inverters": []}
    for path in ("meters", "storage", "inverters"):
        mock_api.get(f"{SITE_URL}/{path}/lifetime-energy", json=empty)

    steps = 1
    while not await coordinator._async_fetch_energy(dt_util.now()):
        steps += 1
        assert steps <= 17, "a month-old gap must be cut to a week"
        freezer.tick(timedelta(minutes=5))


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
@pytest.mark.parametrize(
    ("cached_serial", "site_down_once"),
    [
        # The forced inventory refresh replaces a cache that still has the long serial.
        ("BATT-001-16", False),
        # The refresh fails at startup, so the migration runs on a cache that already
        # lists the short serial; the next poll reads the inventory.
        ("BATT-001", True),
    ],
)
async def test_long_battery_serial_migrates_to_the_short_one(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    area_registry: Any,
    cached_serial: str,
    site_down_once: bool,
) -> None:
    """A battery renamed by the API keeps its entities, and the duplicate disappears."""
    if site_down_once:
        site_calls: list[URL] = []

        async def _down_once(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
            site_calls.append(url)
            if len(site_calls) == 1:
                return AiohttpClientMockResponse(method, url, status=500, text="Server error")
            return AiohttpClientMockResponse(method, url, json=load_json("site.json"))

        # Registered first, so it answers before the default mock.
        mock_api._mocks.insert(0, AiohttpClientMockResponse("GET", URL(SITE_URL)))
        mock_api._mocks[0].side_effect = _down_once
    devices, entities = dr.async_get(hass), er.async_get(hass)
    area = area_registry.async_create("Garage")

    original = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "BATT-001-16")},
        name="Battery BATT-001-16",
    )
    devices.async_update_device(original.id, area_id=area.id)
    kept = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{SITE_ID}_BATT-001-16_state_of_charge",
        suggested_object_id="garage_battery_state_of_charge",
        config_entry=config_entry,
        device_id=original.id,
    )
    duplicate = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, "BATT-001")}, name="Battery"
    )
    dropped = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{SITE_ID}_BATT-001_state_of_charge",
        suggested_object_id="battery_batt_001_state_of_charge",
        config_entry=config_entry,
        device_id=duplicate.id,
    )
    # Storage from 0.1.4: no inventory version, and the frozen long serial still in it.
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.{config_entry.entry_id}",
        "data": {
            "site": {"site_id": SITE_ID, "name": "Home"},
            "devices": [
                {"serial_number": cached_serial, "type": "BATTERY", "name": "Battery"},
            ],
            "inventory_updated": dt_util.now().isoformat(),
            "battery_values": {
                "BATT-001-16": {"state_of_charge": 17.9},
                "BATT-001": {"state_of_charge": 18.2},
            },
            "soc_scales": {"BATT-001-16": "fraction", "BATT-001": "fraction"},
        },
    }

    await _setup(hass, config_entry)

    coordinator = config_entry.runtime_data
    assert coordinator.battery_serials == ["BATT-001"]
    assert set(coordinator.soc_scales) == {"BATT-001"}
    assert entities.async_get(dropped.entity_id) is None
    assert devices.async_get(duplicate.id) is None

    migrated = entities.async_get(kept.entity_id)
    assert migrated.unique_id == f"{SITE_ID}_BATT-001_state_of_charge"
    device = devices.async_get(original.id)
    assert device.identifiers == {(DOMAIN, "BATT-001")}
    assert device.area_id == area.id
    # The surviving entity is live again.
    assert float(_state(hass, kept.entity_id)) == pytest.approx(100)


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_retired_battery_is_ignored(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """A battery marked inactive gets no device, even with a value stored for it."""
    inventory = [
        *load_json("devices.json"),
        {"type": "BATTERY", "serialNumber": "BATT-OLD", "name": "Old", "active": False},
    ]
    mock_api.clear_requests()
    mock_api.get(f"{SITE_URL}/devices", json=inventory)
    mock_api.get(SITE_URL, json=load_json("site.json"))
    for path, fixture in (
        ("meters/telemetry", "meters_telemetry.json"),
        ("storage/telemetry", "storage_telemetry.json"),
        ("meters/lifetime-energy", "meters_lifetime.json"),
        ("storage/lifetime-energy", "storage_lifetime.json"),
        ("inverters/status", "inverters_status.json"),
        ("storage/status", "storage_status.json"),
    ):
        mock_api.get(f"{SITE_URL}/{path}", json=load_json(fixture))

    await _setup(hass, config_entry)
    assert config_entry.runtime_data.battery_serials == ["BATT-001"]
    identifiers = {
        serial
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
        for _, serial in device.identifiers
    }
    assert "BATT-OLD" not in identifiers


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_ev_chargers_and_environmental_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Sites with these devices pay for them; each adds entities of its own."""
    inventory = [
        *load_json("devices.json"),
        {
            "type": "EV_CHARGER",
            "serialNumber": "99123456-A5",
            "manufacturer": "SolarEdge",
            "model": "ONE_EV_Pro",
            "firmwareVersion": "04.00.70",
            "firmware": None,
            "name": "EV Charger 99123456",
            "active": True,
        },
        {
            "type": "SENSOR",
            "serialNumber": None,
            "id": "67001234_PLANE_OF_ARRAY",
            "measurement": "PLANE_OF_ARRAY",
            "sensorType": "IRRADIANCE",
            "name": "Irradiance",
            "active": True,
        },
    ]
    # Most specific first: the mock answers with the first entry that matches.
    aioclient_mock.get(
        f"{SITE_URL}/ev-chargers/telemetry?resolution=TOTAL", json=load_json("ev_total.json")
    )
    aioclient_mock.get(f"{SITE_URL}/ev-chargers/telemetry", json=load_json("ev_telemetry.json"))
    aioclient_mock.get(f"{SITE_URL}/sensors/telemetry", json=load_json("sensors_telemetry.json"))
    aioclient_mock.get(f"{SITE_URL}/devices", json=inventory)
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    for path, fixture in (
        ("meters/telemetry", "meters_telemetry.json"),
        ("storage/telemetry", "storage_telemetry.json"),
        ("meters/lifetime-energy", "meters_lifetime.json"),
        ("storage/lifetime-energy", "storage_lifetime.json"),
        ("inverters/status", "inverters_status.json"),
        ("storage/status", "storage_status.json"),
    ):
        aioclient_mock.get(f"{SITE_URL}/{path}", json=load_json(fixture))

    await _setup(hass, config_entry)

    assert float(_state(hass, "sensor.ev_charger_99123456_charging_power")) == pytest.approx(7.2105)
    assert float(_state(hass, "sensor.ev_charger_99123456_charging_energy")) == pytest.approx(
        1234.5
    )
    assert float(_state(hass, "sensor.home_plane_of_array_irradiance")) == pytest.approx(830.1)
    # site + devices + 4 telemetry + 2 counters + EV total + 2 status
    assert _state(hass, "sensor.home_api_credits_used") == "11"


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_refused_optional_endpoint_does_not_stop_polling(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A refused status call only disables itself, and the planner stops paying for it."""
    aioclient_mock.get(f"{SITE_URL}/inverters/status", status=403, text='{"title": "Forbidden"}')
    aioclient_mock.get(f"{SITE_URL}/storage/status", status=400, text='{"title": "Bad Request"}')
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    aioclient_mock.get(f"{SITE_URL}/devices", json=load_json("devices.json"))
    for path, fixture in (
        ("meters/telemetry", "meters_telemetry.json"),
        ("storage/telemetry", "storage_telemetry.json"),
        ("meters/lifetime-energy", "meters_lifetime.json"),
        ("storage/lifetime-energy", "storage_lifetime.json"),
    ):
        aioclient_mock.get(f"{SITE_URL}/{path}", json=load_json(fixture))

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert float(_state(hass, "sensor.home_solar_power")) == pytest.approx(5.2)
    coordinator = config_entry.runtime_data
    assert coordinator.capabilities["inverter_status"] is False
    assert coordinator.capabilities["storage_status"] is False
    assert coordinator.status_calls == 0


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_status_polling_can_be_disabled(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """An interval of zero spends no credits on status."""
    hass.config_entries.async_update_entry(config_entry, options={CONF_STATUS_INTERVAL: 0})
    await _setup(hass, config_entry)
    assert _windows(mock_api, "/status") == []
    assert _state(hass, "sensor.home_api_credits_used") == "6"


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_site_without_production_meter_uses_inverter_counters(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Inverter counters carry timestamps, so these sites get hourly solar statistics too."""
    meters = load_json("meters_lifetime.json")
    meters["meters"] = [m for m in meters["meters"] if not m["productionEnergy"]]
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    aioclient_mock.get(f"{SITE_URL}/devices", json=load_json("devices.json"))
    aioclient_mock.get(f"{SITE_URL}/meters/lifetime-energy", json=meters)
    aioclient_mock.get(
        f"{SITE_URL}/inverters/lifetime-energy", json=load_json("inverters_lifetime.json")
    )
    for path, fixture in (
        ("meters/telemetry", "meters_telemetry.json"),
        ("storage/telemetry", "storage_telemetry.json"),
        ("storage/lifetime-energy", "storage_lifetime.json"),
        ("inverters/status", "inverters_status.json"),
        ("storage/status", "storage_status.json"),
    ):
        aioclient_mock.get(f"{SITE_URL}/{path}", json=load_json(fixture))

    await _setup(hass, config_entry)

    assert float(_state(hass, "sensor.home_solar_energy")) == pytest.approx(48_500)
    await async_wait_recording_done(hass)
    stats = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, f"{DOMAIN}:3581845_solar", True, {"sum"}
    )
    assert stats[f"{DOMAIN}:3581845_solar"][0]["sum"] == pytest.approx(48_400)


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_production_meter_is_not_mixed_with_inverter_counters(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A quiet window on a production meter must not switch the source of the statistic."""
    inventory = load_json("devices.json")
    inventory[1]["meterType"] = "Production"
    meters = load_json("meters_lifetime.json")
    meters["meters"] = [m for m in meters["meters"] if not m["productionEnergy"]]
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    aioclient_mock.get(f"{SITE_URL}/devices", json=inventory)
    aioclient_mock.get(f"{SITE_URL}/meters/lifetime-energy", json=meters)
    for path, fixture in (
        ("meters/telemetry", "meters_telemetry.json"),
        ("storage/telemetry", "storage_telemetry.json"),
        ("storage/lifetime-energy", "storage_lifetime.json"),
        ("inverters/status", "inverters_status.json"),
        ("storage/status", "storage_status.json"),
    ):
        aioclient_mock.get(f"{SITE_URL}/{path}", json=load_json(fixture))

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert _windows(aioclient_mock, "/inverters/lifetime-energy") == []


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_devices_that_appear_later_get_entities_without_a_restart(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """An inverter asleep at setup gets its entities the first time status reports it."""
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    coordinator.inverter_status["7E1234AB-CE"] = {"mode": "MPPT"}
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert _state(hass, "sensor.inverter_7e1234ab_ce_mode") == "MPPT"


@pytest.mark.freeze_time(NOW)
@pytest.mark.usefixtures("setup_credentials")
async def test_diagnostics_hide_serial_numbers(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    """Per-device values are labelled, so diagnostics stay safe to share."""
    await _setup(hass, config_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["status_calls"] == 2
    assert diagnostics["inverter_status"]["inverter_0"]["mode"] == "MPPT"
    assert diagnostics["battery_status"]["battery_0"]["state_code"] == 4
    text = str(diagnostics)
    assert "7E1234AB-CD" not in text
    assert "BATT-001" not in text
