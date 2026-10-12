"""Sensors for SolarEdge V2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
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
class SolarEdgeBatterySensorDescription(SensorEntityDescription):
    """Describes a per-battery sensor (key into ``coordinator.battery_values``)."""


BATTERY_SENSORS: tuple[SolarEdgeBatterySensorDescription, ...] = (
    SolarEdgeBatterySensorDescription(
        key="state_of_charge",
        translation_key="pack_state_of_charge",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SolarEdgeBatterySensorDescription(
        key="power",
        translation_key="pack_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    ),
    SolarEdgeBatterySensorDescription(
        key="remaining_energy",
        translation_key="pack_remaining_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SolarEdgeBatterySensorDescription(
        key="usable_capacity",
        translation_key="pack_usable_capacity",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SolarEdge sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        SolarEdgeSensor(coordinator, description)
        for description in SENSORS
        if description.exists_fn(coordinator)
    ]
    if coordinator.has_battery:
        entities.extend(
            SolarEdgeBatterySensor(coordinator, serial, description)
            for serial in coordinator.battery_serials
            for description in BATTERY_SENSORS
        )
    async_add_entities(entities)


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


class SolarEdgeBatterySensor(CoordinatorEntity[SolarEdgeCoordinator], SensorEntity):
    """A sensor for one battery, on its own device."""

    _attr_has_entity_name = True
    entity_description: SolarEdgeBatterySensorDescription

    def __init__(
        self,
        coordinator: SolarEdgeCoordinator,
        serial: str,
        description: SolarEdgeBatterySensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._serial = serial
        self._attr_unique_id = f"{coordinator.site_id}_{serial}_{description.key}"
        device = coordinator.battery_device(serial)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=(device.name if device and device.name else f"Battery {serial}"),
            manufacturer=(device.manufacturer if device and device.manufacturer else "SolarEdge"),
            model=(device.model if device and device.model else "Battery"),
            sw_version=device.firmware if device else None,
            serial_number=serial,
            via_device=(DOMAIN, str(coordinator.site_id)),
        )

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        return self.coordinator.battery_values.get(self._serial, {}).get(
            self.entity_description.key
        )
