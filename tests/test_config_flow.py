"""Tests for the SolarEdge V2 config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.solaredge_v2.const import (
    CONF_BILLING_DAY,
    CONF_DAY_SHARE,
    CONF_ENERGY_INTERVAL,
    CONF_MONTHLY_BUDGET,
    CONF_SITE_ID,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_TOKEN,
)

from .conftest import SITE_ID, SITE_URL, load_json

REDIRECT = "https://example.com/auth/external/callback"
TOKEN = {
    "access_token": "access",
    "token_type": "Bearer",
    "expires_in": 7200,
    "refresh_token": "refresh",
}


async def _authorize(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    result: dict,
) -> dict:
    state = config_entry_oauth2_flow._encode_jwt(
        hass, {"flow_id": result["flow_id"], "redirect_uri": REDIRECT}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(OAUTH2_AUTHORIZE)
    assert "client_id=client-id" in result["url"]
    assert "scope=SITE_DATA+DEVICE_DATA" in result["url"]
    assert "access_duration=24" in result["url"]

    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200

    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    return await hass.config_entries.flow.async_configure(result["flow_id"])


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_full_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """OAuth, then site selection creates the entry."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await _authorize(hass, hass_client_no_auth, aioclient_mock, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"

    # Token exchange is sent as JSON with the client credentials in the body.
    _, _, body, _ = aioclient_mock.mock_calls[-1]
    assert body["grant_type"] == "authorization_code"
    assert body["client_secret"] == "client-secret"

    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    with patch("custom_components.solaredge_v2.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SITE_ID: SITE_ID}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"][CONF_SITE_ID] == SITE_ID
    assert result["data"]["token"]["refresh_token"] == "refresh"
    assert result["result"].unique_id == str(SITE_ID)


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_invalid_site(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A site outside the grant shows an error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await _authorize(hass, hass_client_no_auth, aioclient_mock, result)
    aioclient_mock.get(f"{SITE_URL}9", status=403, text="{}")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: 35818459}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_site"}


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_reauth(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Reauth stores the new token on the existing entry."""
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    with patch("custom_components.solaredge_v2.async_setup_entry", return_value=True):
        result = await _authorize(hass, hass_client_no_auth, aioclient_mock, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["token"]["access_token"] == "access"
    assert config_entry.data[CONF_SITE_ID] == SITE_ID


async def test_options(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Budget options are saved."""
    with patch("custom_components.solaredge_v2.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_MONTHLY_BUDGET: 1500,
                CONF_BILLING_DAY: 15,
                CONF_DAY_SHARE: 80,
                CONF_ENERGY_INTERVAL: 4,
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_MONTHLY_BUDGET] == 1500
