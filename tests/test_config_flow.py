"""Tests for the SolarEdge API Connector config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from yarl import URL

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
from custom_components.solaredge_v2.oauth import OAUTH_CALLBACK_PATH

from .conftest import SITE_ID, SITE_URL, load_json

PASTED = f"https://ha.local:8123/?code=fresh&site_id={SITE_ID}"
TOKEN = {
    "access_token": "access",
    "token_type": "Bearer",
    "expires_in": 7200,
    "refresh_token": "refresh",
}
SETUP = "custom_components.solaredge_v2.async_setup_entry"


async def _menu(hass: HomeAssistant, flow_id: str | None = None) -> dict:
    """Start (or resume, for reauth) the flow up to the sign-in menu."""
    if flow_id is None:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    else:
        result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "sign_in"
    assert result["description_placeholders"]["callback_url"].endswith(OAUTH_CALLBACK_PATH)
    return result


async def _authorize(hass: HomeAssistant, flow_id: str | None = None) -> tuple[str, str]:
    """Pick automatic sign-in; return the flow id and the external_id sent to SolarEdge."""
    result = await _menu(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "authorize"}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    url = URL(result["url"])
    assert str(url).startswith(OAUTH2_AUTHORIZE)
    assert url.query["client_id"] == "client-id"
    assert url.query["scope"] == "SITE_DATA DEVICE_DATA"
    assert url.query["access_duration"] == "24"
    assert url.query["redirect_uri"].endswith(OAUTH_CALLBACK_PATH)
    return result["flow_id"], url.query["external_id"]


async def _redirect(
    hass_client_no_auth: ClientSessionGenerator, query: str, status: int = 200
) -> str:
    """Simulate SolarEdge redirecting the browser to our callback."""
    client = await hass_client_no_auth()
    resp = await client.get(f"{OAUTH_CALLBACK_PATH}?{query}")
    assert resp.status == status
    assert resp.content_type == "text/html"
    return await resp.text()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (PASTED, {"code": "fresh", "site_id": str(SITE_ID)}),
        ("  http://ha.local:8123/auth/external/callback?code=x&state=y  ", {"code": "x"}),
        ("code=x&site_id=5", {"code": "x", "site_id": "5"}),
        ("bareCode123", {"code": "bareCode123"}),
        ("https://ha.local/?error=access_denied", {"error": "access_denied"}),
        ("", {}),
    ],
)
def test_parse_redirect(value: str, expected: dict) -> None:
    """Pasted addresses, query strings and bare codes are understood."""
    assert parse_redirect(value) == expected


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_full_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """SolarEdge redirects to our callback with code + site_id; no site prompt needed."""
    flow_id, external_id = await _authorize(hass)
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))

    text = await _redirect(
        hass_client_no_auth, f"code=abcd&site_id={SITE_ID}&external_id={external_id}"
    )
    assert "connected" in text
    assert "window.close" in text

    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"][CONF_SITE_ID] == SITE_ID
    assert result["data"]["token"]["refresh_token"] == "refresh"
    assert result["data"]["token"]["expires_at"] > 0
    assert result["result"].unique_id == str(SITE_ID)

    # Token exchange is sent as JSON, client credentials in the body, no redirect_uri.
    body = next(c for c in aioclient_mock.mock_calls if str(c[1]) == OAUTH2_TOKEN)[2]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "abcd"
    assert body["client_secret"] == "client-secret"
    assert "redirect_uri" not in body


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_callback_without_external_id(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A redirect without external_id is accepted when only one sign-in is pending."""
    flow_id, _ = await _authorize(hass)
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    await _redirect(hass_client_no_auth, "code=abcd")
    result = await hass.config_entries.flow.async_configure(flow_id)
    # No site_id in the redirect, so ask for it.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_callback_unknown_external_id(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An unknown external_id is rejected and the flow keeps waiting."""
    flow_id, _ = await _authorize(hass)
    text = await _redirect(hass_client_no_auth, "code=abcd&external_id=forged", status=400)
    assert "setup" in text
    assert aioclient_mock.call_count == 0
    progress = hass.config_entries.flow.async_get(flow_id)
    assert progress["step_id"] == "authorize"


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_callback_access_denied(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """Declining at SolarEdge aborts the flow."""
    flow_id, external_id = await _authorize(hass)
    await _redirect(hass_client_no_auth, f"error=access_denied&external_id={external_id}")
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "user_rejected_authorize"


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_refused_code_falls_back_to_paste(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A refused code shows the paste form; pasting a fresh redirect finishes setup."""
    flow_id, external_id = await _authorize(hass)
    aioclient_mock.post(
        OAUTH2_TOKEN,
        status=400,
        json={"error": "invalid_request", "error_description": "Invalid code"},
    )
    await _redirect(hass_client_no_auth, f"code=abcd&external_id={external_id}")
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "paste"
    assert result["errors"] == {"base": "oauth_failed"}
    assert result["description_placeholders"]["error_detail"] == "invalid_request: Invalid code"
    aioclient_mock.clear_requests()

    # The refused code is never sent again.
    await hass.config_entries.flow.async_configure(
        flow_id, {"redirect_url": "https://ha.local/?code=abcd"}
    )
    assert aioclient_mock.call_count == 0

    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, {"redirect_url": PASTED})
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_manual_sign_in(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Choosing manual sign-in and pasting the dashboard address works without a site prompt."""
    result = await _menu(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "paste"}
    )
    assert result["step_id"] == "paste"
    assert "external_id=" in result["description_placeholders"]["authorize_url"]

    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result2["errors"] == {"base": "missing_code"}

    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    aioclient_mock.get(SITE_URL, json=load_json("site.json"))
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": PASTED}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SITE_ID] == SITE_ID


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_invalid_site(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A site outside the grant shows an error on the site step."""
    flow_id, external_id = await _authorize(hass)
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    aioclient_mock.get(f"{SITE_URL}9", status=403, text="{}")
    await _redirect(hass_client_no_auth, f"code=abcd&site_id=35818459&external_id={external_id}")
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"
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
    flow_id, external_id = await _authorize(hass, result["flow_id"])
    aioclient_mock.post(OAUTH2_TOKEN, json={**TOKEN, "access_token": "new"})
    await _redirect(hass_client_no_auth, f"code=abcd&site_id={SITE_ID}&external_id={external_id}")
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["token"]["access_token"] == "new"
    assert config_entry.data[CONF_SITE_ID] == SITE_ID


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_reauth_wrong_site(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """Signing in to another site during reauth is refused."""
    result = await config_entry.start_reauth_flow(hass)
    flow_id, external_id = await _authorize(hass, result["flow_id"])
    aioclient_mock.post(OAUTH2_TOKEN, json=TOKEN)
    await _redirect(hass_client_no_auth, f"code=abcd&site_id=1&external_id={external_id}")
    result = await hass.config_entries.flow.async_configure(flow_id)
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
