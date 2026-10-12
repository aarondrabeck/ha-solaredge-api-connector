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

from custom_components.solaredge_v2.config_flow import parse_redirect
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

PASTED = f"https://my.home-assistant.io/redirect/oauth?site_id={SITE_ID}&code=abcd"
TOKEN = {
    "access_token": "access",
    "token_type": "Bearer",
    "expires_in": 7200,
    "refresh_token": "refresh",
}
SETUP = "custom_components.solaredge_v2.async_setup_entry"


async def _start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth"
    url = result["description_placeholders"]["authorize_url"]
    assert url.startswith(OAUTH2_AUTHORIZE)
    assert "client_id=client-id" in url
    assert "scope=SITE_DATA+DEVICE_DATA" in url
    assert "access_duration=24" in url
    return result


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (PASTED, {"code": "abcd", "site_id": str(SITE_ID)}),
        ("  http://ha.local:8123/auth/external/callback?code=x&state=y  ", {"code": "x"}),
        ("code=x&site_id=5", {"code": "x", "site_id": "5"}),
        ("bareCode123", {"code": "bareCode123"}),
        (
            "https://my.home-assistant.io/redirect/oauth?error=access_denied",
            {"error": "access_denied"},
        ),
        ("", {}),
    ],
)
def test_parse_redirect(value: str, expected: dict) -> None:
    """Pasted addresses, query strings and bare codes are understood."""
    assert parse_redirect(value) == expected


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_paste_redirect_creates_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Pasting the redirect address (code + site_id) finishes setup in one step."""
    result = await _start(hass)
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": PASTED}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"][CONF_SITE_ID] == SITE_ID
    assert result["data"]["token"]["refresh_token"] == "refresh"
    assert result["data"]["token"]["expires_at"] > 0
    assert result["result"].unique_id == str(SITE_ID)

    # Token exchange is sent as JSON with the client credentials in the body.
    token_call = next(c for c in aioclient_mock.mock_calls if str(c[1]) == OAUTH2_TOKEN)
    body = token_call[2]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "abcd"
    assert body["client_secret"] == "client-secret"
    assert body["redirect_uri"]


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_callback_with_state_then_submit(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """If SolarEdge echoes state, HA's callback stores the code; Submit continues."""
    result = await _start(hass)
    state = config_entry_oauth2_flow._encode_jwt(
        hass,
        {
            "flow_id": result["flow_id"],
            "redirect_uri": "https://example.com/auth/external/callback",
        },
    )
    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200

    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    # HA's callback drops site_id, so ask for it.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"

    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SITE_ID: SITE_ID}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_auth_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Missing code, declined access and a rejected code keep the form open."""
    result = await _start(hass)
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["errors"] == {"base": "missing_code"}

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"redirect_url": "https://my.home-assistant.io/redirect/oauth?error=denied"}
    )
    assert result["errors"] == {"base": "authorize_rejected"}

    aioclient_mock.post(OAUTH2_TOKEN, status=400, json={"error": "invalid_request"})
    result = await hass.config_entries.flow.async_configure(flow_id, {"redirect_url": PASTED})
    assert result["step_id"] == "auth"
    assert result["errors"] == {"base": "oauth_failed"}


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_invalid_site(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A site outside the grant shows an error on the site step."""
    result = await _start(hass)
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    aioclient_mock.get(f"{SITE_URL}9", status=403, text="{}")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"redirect_url": "https://my.home-assistant.io/redirect/oauth?code=abcd&site_id=35818459"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
    assert result["errors"] == {"base": "invalid_site"}


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Reauth stores the new token on the existing entry."""
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "auth"
    aioclient_mock.post(OAUTH2_TOKEN, json={**TOKEN, "access_token": "new"})
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": PASTED}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["token"]["access_token"] == "new"
    assert config_entry.data[CONF_SITE_ID] == SITE_ID


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_reauth_wrong_site(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Signing in to another site during reauth is refused."""
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"redirect_url": "https://my.home-assistant.io/redirect/oauth?code=abcd&site_id=1"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_site"


async def test_options(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Budget options are saved."""
    with patch(SETUP, return_value=True):
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
