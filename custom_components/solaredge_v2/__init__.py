"""SolarEdge V2: SolarEdge ONE Developer Platform (Monitoring API v2) for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    OAuth2Session,
    async_get_config_entry_implementation,
)

from .coordinator import SolarEdgeCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

type SolarEdgeConfigEntry = ConfigEntry[SolarEdgeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Set up SolarEdge V2 from a config entry."""
    try:
        implementation = await async_get_config_entry_implementation(hass, entry)
    except ImplementationUnavailableError as err:
        raise ConfigEntryNotReady("OAuth implementation not available") from err
    session = OAuth2Session(hass, entry, implementation)

    async def _access_token() -> str:
        await session.async_ensure_token_valid()
        return str(session.token["access_token"])

    coordinator = SolarEdgeCoordinator(hass, entry, _access_token)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
