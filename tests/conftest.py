"""Shared fixtures for SolarEdge API Connector tests."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.solaredge_v2.const import API_BASE_URL, CONF_SITE_ID, DOMAIN

SITE_ID = 3581845
SITE_URL = f"{API_BASE_URL}/sites/{SITE_ID}"
FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def mock_recorder_before_hass(async_test_recorder: None) -> None:
    """Prepare the recorder database before hass starts."""


@pytest.fixture(autouse=True)
def auto_recorder(recorder_mock: None) -> None:
    """The integration depends on recorder; it must start before hass."""


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading the custom integration in all tests."""


@pytest.fixture
async def setup_credentials(hass: HomeAssistant) -> None:
    """Register application credentials."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client-id", "client-secret"), DOMAIN
    )


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A configured entry with a valid token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        unique_id=str(SITE_ID),
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expires_in": 7200,
                "expires_at": time.time() + 7200,
            },
            CONF_SITE_ID: SITE_ID,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_api(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Mock every endpoint the integration polls."""
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    aioclient_mock.get(f"{SITE_URL}/devices", json=load_json("devices.json"))
    aioclient_mock.get(f"{SITE_URL}/meters/telemetry", json=load_json("meters_telemetry.json"))
    aioclient_mock.get(f"{SITE_URL}/storage/telemetry", json=load_json("storage_telemetry.json"))
    aioclient_mock.get(f"{SITE_URL}/meters/lifetime-energy", json=load_json("meters_lifetime.json"))
    aioclient_mock.get(
        f"{SITE_URL}/storage/lifetime-energy", json=load_json("storage_lifetime.json")
    )
    return aioclient_mock
