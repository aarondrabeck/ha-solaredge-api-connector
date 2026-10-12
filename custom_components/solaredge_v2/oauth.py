"""OAuth callback for SolarEdge.

SolarEdge redirects to the app's registered address with ``code`` and
``site_id`` but never returns ``state``, so neither HA's generic OAuth
callback nor my.home-assistant.io can be used. Like the core Plex
integration, we serve our own callback and correlate the redirect with the
config flow through SolarEdge's ``external_id`` parameter.
"""

from __future__ import annotations

from html import escape
import logging

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers import http
from homeassistant.helpers.config_entry_oauth2_flow import HEADER_FRONTEND_BASE
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

OAUTH_CALLBACK_PATH = "/auth/solaredge_v2/callback"
DATA_PENDING = f"{DOMAIN}_oauth_pending"
DATA_VIEW = f"{DOMAIN}_oauth_view"


def callback_url(hass: HomeAssistant) -> str:
    """Return the callback address as the user's browser reaches Home Assistant."""
    request = http.current_request.get()
    if request is not None and (base := request.headers.get(HEADER_FRONTEND_BASE)):
        return f"{base.rstrip('/')}{OAUTH_CALLBACK_PATH}"
    try:
        return f"{get_url(hass)}{OAUTH_CALLBACK_PATH}"
    except NoURLAvailableError:
        return f"http://homeassistant.local:8123{OAUTH_CALLBACK_PATH}"


def pending_flows(hass: HomeAssistant) -> dict[str, str]:
    """``external_id`` → config flow id for sign-ins in progress."""
    return hass.data.setdefault(DATA_PENDING, {})


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Serve the callback (once per HA run)."""
    if not hass.data.get(DATA_VIEW):
        hass.http.register_view(SolarEdgeOAuthCallbackView)
        hass.data[DATA_VIEW] = True


def _page(message: str, status: int = 200, *, close: bool = False) -> web.Response:
    script = "<script>window.close()</script>" if close else ""
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>SolarEdge API Connector</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;"
        f"padding:0 1rem'>{script}<h2>SolarEdge API Connector</h2><p>{escape(message)}</p>"
        "</body></html>"
    )
    return web.Response(status=status, content_type="text/html", text=body)


class SolarEdgeOAuthCallbackView(HomeAssistantView):
    """Receive SolarEdge's redirect and resume the matching config flow."""

    url = OAUTH_CALLBACK_PATH
    name = "auth:solaredge_v2:callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Hand the authorization code to the config flow."""
        hass = request.app[KEY_HASS]
        query = request.query
        flows = pending_flows(hass)
        external_id = query.get("external_id")
        if external_id:
            flow_id = flows.get(external_id)
        else:
            # Some app settings redirect without external_id; accept only when unambiguous.
            flow_id = next(iter(flows.values())) if len(flows) == 1 else None
        if flow_id is None:
            return _page(
                "This sign-in doesn't match a setup in progress (it may have expired). "
                "Start the SolarEdge API Connector setup again in Home Assistant.",
                400,
            )

        user_input = {"site_id": query.get("site_id", "")}
        if code := query.get("code"):
            user_input["code"] = code
        elif error := query.get("error"):
            user_input["error"] = error
        else:
            return _page("SolarEdge did not send an authorization code.", 400)

        for key in [k for k, v in flows.items() if v == flow_id]:
            flows.pop(key)
        try:
            await hass.config_entries.flow.async_configure(flow_id=flow_id, user_input=user_input)
        except UnknownFlow:
            return _page(
                "The setup dialog was closed. Start the SolarEdge API Connector setup again.",
                400,
            )
        _LOGGER.debug("Resumed SolarEdge OAuth flow %s", flow_id)
        if "error" in user_input:
            return _page("Access was declined. You can close this tab.", close=True)
        return _page("SolarEdge connected. You can close this tab.", close=True)
