"""Sensors for SolarEdge API Connector."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfIrradiance,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SolarEdgeConfigEntry
from .const import DOMAIN, IDLE_THRESHOLD_W
from .coordinator import SolarEdgeCoordinator

type StateType = float | str | datetime | None


def _flow_state(value: float | None, positive: str, negative: str) -> str | None:
    if value is None:
        return None
    if value > IDLE_THRESHOLD_W:
        return positive
    if value < -IDLE_THRESHOLD_W:
        return negative
    return "idle"


@dataclass(frozen=True, kw_only=True)
class SolarEdgeSensorDescription(SensorEntityDescription):
    """Describes a SolarEdge sensor."""

    value_fn: Callable[[SolarEdgeCoordinator], StateType]
    exists_fn: Callable[[SolarEdgeCoordinator], bool] = lambda _: True


def _value(key: str) -> Callable[[SolarEdgeCoordinator], float | None]:
    return lambda coordinator: coordinator.values.get(key)


def _battery(coordinator: SolarEdgeCoordinator) -> bool:
    return coordinator.has_battery


def _has(key: str) -> Callable[[SolarEdgeCoordinator], bool]:
    return lambda coordinator: key in coordinator.values


def _power(key: str, **kwargs) -> SolarEdgeSensorDescription:
    return SolarEdgeSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=_value(key),
        **kwargs,
    )


def _energy(key: str, **kwargs) -> SolarEdgeSensorDescription:
    return SolarEdgeSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=_value(key),
        **kwargs,
    )


SENSORS: tuple[SolarEdgeSensorDescription, ...] = (
    # Power (HA energy-dashboard polarity: grid + import, battery + discharge)
    _power("solar_power"),
    _power("home_power", exists_fn=_has("home_power")),
    _power("grid_power", exists_fn=_has("grid_power")),
    _power("grid_import_power", exists_fn=_has("grid_import_power")),
    _power("grid_export_power", exists_fn=_has("grid_export_power")),
    _power("battery_power", exists_fn=_battery),
    _power("battery_charge_power", exists_fn=_battery),
    _power("battery_discharge_power", exists_fn=_battery),
    SolarEdgeSensorDescription(
        key="battery_state_of_charge",
        translation_key="battery_state_of_charge",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=_value("battery_state_of_charge"),
        exists_fn=_battery,
    ),
    SolarEdgeSensorDescription(
        key="battery_remaining_energy",
        translation_key="battery_remaining_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_value("battery_remaining_energy"),
        exists_fn=_battery,
    ),
    SolarEdgeSensorDescription(
        key="battery_usable_capacity",
        translation_key="battery_usable_capacity",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_value("battery_usable_capacity"),
        exists_fn=_battery,
    ),
    SolarEdgeSensorDescription(
        key="grid_status",
        translation_key="grid_status",
        device_class=SensorDeviceClass.ENUM,
        options=["importing", "exporting", "idle"],
        value_fn=lambda c: _flow_state(c.values.get("grid_power"), "importing", "exporting"),
        exists_fn=_has("grid_power"),
    ),
    SolarEdgeSensorDescription(
        key="battery_status",
        translation_key="battery_status",
        device_class=SensorDeviceClass.ENUM,
        options=["discharging", "charging", "idle"],
        value_fn=lambda c: _flow_state(c.values.get("battery_power"), "discharging", "charging"),
        exists_fn=_battery,
    ),
    # Lifetime energy counters
    _energy("solar_energy"),
    _energy("home_energy", exists_fn=_has("home_energy")),
    _energy("grid_import_energy", exists_fn=_has("grid_import_energy")),
    _energy("grid_export_energy", exists_fn=_has("grid_export_energy")),
    _energy("battery_charge_energy", exists_fn=_battery),
    _energy("battery_discharge_energy", exists_fn=_battery),
    # Credit budget diagnostics
    SolarEdgeSensorDescription(
        key="credits_used",
        translation_key="credits_used",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.ledger.used,
    ),
    SolarEdgeSensorDescription(
        key="credits_remaining",
        translation_key="credits_remaining",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: max(c.budget_config.monthly_budget - c.ledger.used, 0),
    ),
    SolarEdgeSensorDescription(
        key="credits_projected",
        translation_key="credits_projected",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.poll_plan.projected_total if c.poll_plan else None,
    ),
    SolarEdgeSensorDescription(
        key="data_timestamp",
        translation_key="data_timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data_timestamp,
    ),
    SolarEdgeSensorDescription(
        key="next_update",
        translation_key="next_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.next_update,
    ),
)


@dataclass(frozen=True, kw_only=True)
class SolarEdgeDeviceSensorDescription(SensorEntityDescription):
    """Describes a sensor of one device; ``key`` indexes that device's ``source`` values."""

    source: Callable[[SolarEdgeCoordinator], dict[str, dict[str, Any]]] = lambda c: c.battery_values


BATTERY_SENSORS: tuple[SolarEdgeDeviceSensorDescription, ...] = (
    SolarEdgeDeviceSensorDescription(
        key="state_of_charge",
        translation_key="pack_state_of_charge",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SolarEdgeDeviceSensorDescription(
        key="power",
        translation_key="pack_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    ),
    SolarEdgeDeviceSensorDescription(
        key="remaining_energy",
        translation_key="pack_remaining_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SolarEdgeDeviceSensorDescription(
        key="usable_capacity",
        translation_key="pack_usable_capacity",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SolarEdgeDeviceSensorDescription(
        key="internal_temperature",
        translation_key="pack_internal_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        source=lambda c: c.battery_status,
    ),
    # SolarEdge publishes no meaning for this code, so it starts disabled.
    SolarEdgeDeviceSensorDescription(
        key="state_code",
        translation_key="pack_state_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        source=lambda c: c.battery_status,
    ),
)

INVERTER_SENSORS: tuple[SolarEdgeDeviceSensorDescription, ...] = (
    SolarEdgeDeviceSensorDescription(
        key="mode",
        translation_key="inverter_mode",
        source=lambda c: c.inverter_status,
    ),
    SolarEdgeDeviceSensorDescription(
        key="temperature",
        translation_key="inverter_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        source=lambda c: c.inverter_status,
    ),
    SolarEdgeDeviceSensorDescription(
        key="power_limit",
        translation_key="inverter_power_limit",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        source=lambda c: c.inverter_status,
    ),
    # A raw device code; the decoded state is the mode sensor.
    SolarEdgeDeviceSensorDescription(
        key="operation_mode",
        translation_key="inverter_operation_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        source=lambda c: c.inverter_status,
    ),
)

EV_CHARGER_SENSORS: tuple[SolarEdgeDeviceSensorDescription, ...] = (
    SolarEdgeDeviceSensorDescription(
        key="power",
        translation_key="charger_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        source=lambda c: c.ev_values,
    ),
    SolarEdgeDeviceSensorDescription(
        key="energy",
        translation_key="charger_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        source=lambda c: c.ev_values,
    ),
)


def _environment(
    measurement: str,
    device_class: SensorDeviceClass,
    unit: str,
    state_class: SensorStateClass = SensorStateClass.MEASUREMENT,
) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=measurement.lower(),
        translation_key=measurement.lower(),
        device_class=device_class,
        state_class=state_class,
        native_unit_of_measurement=unit,
    )


# Keyed by the inventory's ``measurement``; each sensor reports exactly one.
ENVIRONMENT_SENSORS: dict[str, SensorEntityDescription] = {
    measurement: _environment(measurement, device_class, unit, *state_class)
    for measurement, device_class, unit, *state_class in (
        (
            "PLANE_OF_ARRAY",
            SensorDeviceClass.IRRADIANCE,
            UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        ),
        (
            "GLOBAL_HORIZONTAL",
            SensorDeviceClass.IRRADIANCE,
            UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        ),
        ("DIRECT", SensorDeviceClass.IRRADIANCE, UnitOfIrradiance.WATTS_PER_SQUARE_METER),
        ("DIFFUSED", SensorDeviceClass.IRRADIANCE, UnitOfIrradiance.WATTS_PER_SQUARE_METER),
        ("MODULE", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
        ("AMBIENT", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
        ("WIND_SPEED", SensorDeviceClass.WIND_SPEED, UnitOfSpeed.METERS_PER_SECOND),
        (
            "WIND_DIRECTION",
            SensorDeviceClass.WIND_DIRECTION,
            DEGREE,
            SensorStateClass.MEASUREMENT_ANGLE,
        ),
    )
}


def _all_entities(coordinator: SolarEdgeCoordinator) -> list[SensorEntity]:
    """Build every entity the site currently supports."""
    entities: list[SensorEntity] = [
        SolarEdgeSensor(coordinator, description)
        for description in SENSORS
        if description.exists_fn(coordinator)
    ]
    batteries = coordinator.battery_serials if coordinator.has_battery else []
    chargers = coordinator.ev_charger_serials if coordinator.has_ev_charger else []
    device_groups = (
        (batteries, BATTERY_SENSORS, "Battery"),
        (coordinator.inverter_status, INVERTER_SENSORS, "Inverter"),
        (chargers, EV_CHARGER_SENSORS, "EV charger"),
    )
    entities.extend(
        SolarEdgeDeviceSensor(coordinator, serial, description, kind)
        for serials, descriptions, kind in device_groups
        for serial in serials
        for description in descriptions
    )
    if coordinator.has_env_sensors:
        entities.extend(
            SolarEdgeEnvironmentSensor(
                coordinator, device.serial_number, ENVIRONMENT_SENSORS[device.measurement]
            )
            for device in coordinator.active_devices("SENSOR")
            if device.measurement in ENVIRONMENT_SENSORS
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SolarEdge sensors, adding more as new devices and values appear."""
    coordinator = entry.runtime_data
    known: set[str | None] = set()

    @callback
    def _add_new_entities() -> None:
        entities = [e for e in _all_entities(coordinator) if e.unique_id not in known]
        known.update(entity.unique_id for entity in entities)
        if entities:
            async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class SolarEdgeSensor(CoordinatorEntity[SolarEdgeCoordinator], SensorEntity):
    """A SolarEdge site sensor."""

    _attr_has_entity_name = True
    entity_description: SolarEdgeSensorDescription

    def __init__(
        self, coordinator: SolarEdgeCoordinator, description: SolarEdgeSensorDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.site_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, str(coordinator.site_id))})

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator)


class SolarEdgeDeviceSensor(CoordinatorEntity[SolarEdgeCoordinator], SensorEntity):
    """A sensor for one battery, inverter or EV charger, on that device."""

    _attr_has_entity_name = True
    entity_description: SolarEdgeDeviceSensorDescription

    def __init__(
        self,
        coordinator: SolarEdgeCoordinator,
        serial: str,
        description: SolarEdgeDeviceSensorDescription,
        kind: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._serial = serial
        self._attr_unique_id = f"{coordinator.site_id}_{serial}_{description.key}"
        device = coordinator.inventory_device(serial)
        # The API can call every battery just "Battery", which tells them apart from nothing.
        name = device.name if device and device.name not in (None, kind) else f"{kind} {serial}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=name,
            manufacturer=(device.manufacturer if device and device.manufacturer else "SolarEdge"),
            model=(device.model if device and device.model else kind),
            sw_version=device.firmware if device else None,
            serial_number=serial,
        )
        # HA 2026.10 links the parent by registry id; the identifier form is deprecated.
        if coordinator.site_device_id:
            self._attr_device_info["via_device_id"] = coordinator.site_device_id

    @property
    def native_value(self) -> float | str | None:
        """Return the sensor value."""
        values = self.entity_description.source(self.coordinator)
        return values.get(self._serial, {}).get(self.entity_description.key)


class SolarEdgeEnvironmentSensor(CoordinatorEntity[SolarEdgeCoordinator], SensorEntity):
    """An environmental sensor; each measures one thing, so it sits on the site device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SolarEdgeCoordinator,
        sensor_id: str,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._sensor_id = sensor_id
        self._attr_unique_id = f"{coordinator.site_id}_{sensor_id}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, str(coordinator.site_id))})

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        return self.coordinator.sensor_values.get(self._sensor_id)
