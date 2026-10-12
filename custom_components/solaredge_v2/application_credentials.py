"""Application credentials for SolarEdge API Connector (OAuth 2.0 Site Access)."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from aiohttp import ClientError, ClientResponseError
from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestConnectionError,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEVELOPER_PORTAL_URL, OAUTH2_AUTHORIZE, OAUTH2_TOKEN
from .oauth import callback_url

_LOGGER = logging.getLogger(__name__)

AUTHORIZATION_SERVER = AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


def _error_detail(body: str) -> str:
    """Return ``error: description`` from an RFC 6749 error body."""
    try:
        payload = json.loads(body)
    except ValueError:
        return body[:200]
    if not isinstance(payload, dict):
        return body[:200]
    error = payload.get("error") or payload.get("message") or "error"
    description = payload.get("error_description")
    return f"{error}: {description}" if description else str(error)


class SolarEdgeOAuth2Implementation(AuthImplementation):
    """SolarEdge expects the token request as a JSON body, not a form."""

    async def _token_request(self, data: dict) -> dict:
        # SolarEdge's token reference takes no redirect_uri (the registered one is
        # used), and a value that differs from the registered URI gets the code refused.
        data.pop("redirect_uri", None)
        data["client_id"] = self.client_id
        data["client_secret"] = self.client_secret
        session = async_get_clientsession(self.hass)
        detail = ""
        try:
            async with session.post(self.token_url, json=data) as resp:
                if resp.status >= 400:
                    detail = _error_detail(await resp.text())
                    _LOGGER.warning(
                        "SolarEdge token request (%s) failed with HTTP %s: %s",
                        data.get("grant_type"),
                        resp.status,
                        detail,
                    )
                resp.raise_for_status()
                return cast(dict[str, Any], await resp.json(content_type=None))
        except ClientResponseError as err:
            kwargs: dict[str, Any] = {
                "request_info": err.request_info,
                "history": err.history,
                "status": err.status,
                "message": detail or err.message,
                "headers": err.headers,
                "domain": self.domain,
            }
            if err.status == 429 or err.status >= 500:
                raise OAuth2TokenRequestTransientError(**kwargs) from err
            if 400 <= err.status < 500:
                # invalid_request / invalid_client / expired grant: user must re-authorize.
                raise OAuth2TokenRequestReauthError(**kwargs) from err
            raise OAuth2TokenRequestError(**kwargs) from err
        except ClientError as err:
            raise OAuth2TokenRequestConnectionError(domain=self.domain) from err


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the SolarEdge authorization server."""
    return AUTHORIZATION_SERVER


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return the JSON-body OAuth implementation."""
    return SolarEdgeOAuth2Implementation(hass, auth_domain, credential, AUTHORIZATION_SERVER)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Placeholders for the application credentials dialog."""
    return {
        "developer_url": DEVELOPER_PORTAL_URL,
        "redirect_url": callback_url(hass),
    }
