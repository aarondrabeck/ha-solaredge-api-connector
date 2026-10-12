"""Config flow for SolarEdge API Connector."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.exceptions import OAuth2TokenRequestError
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol
from yarl import URL

from .api import (
    SolarEdgeAuthError,
    SolarEdgeClient,
    SolarEdgeError,
    SolarEdgeForbiddenError,
    SolarEdgeNotFoundError,
)
from .const import (
    CONF_BILLING_DAY,
    CONF_DAY_SHARE,
    CONF_ENERGY_INTERVAL,
    CONF_MONTHLY_BUDGET,
    CONF_SITE_ID,
    DEFAULT_BILLING_DAY,
    DEFAULT_DAY_SHARE,
    DEFAULT_ENERGY_INTERVAL,
    DEFAULT_MONTHLY_BUDGET,
    DOMAIN,
    OAUTH2_ACCESS_DURATION,
    OAUTH2_AUTHORIZE,
    OAUTH2_SCOPES,
)
from .oauth import async_register_view, callback_url, pending_flows

if TYPE_CHECKING:
    from . import SolarEdgeConfigEntry

CONF_REDIRECT_URL = "redirect_url"


def parse_redirect(value: str) -> dict[str, str]:
    """Read ``code``/``site_id``/``error`` from a pasted redirect URL (or a bare code)."""
    value = value.strip()
    if not value:
        return {}
    if "=" not in value and "/" not in value:
        return {"code": value}
    query = URL(value).query if "?" in value else URL(f"?{value.lstrip('?')}").query
    return {key: query[key] for key in ("code", "site_id", "error") if query.get(key)}


class OAuth2FlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Authorize with SolarEdge, then pick the site the grant covers."""

    DOMAIN = DOMAIN
    VERSION = 1

    _oauth_data: dict[str, Any]
    _authorize_url: str | None = None
    _site_from_redirect: int | None = None
    _token: dict[str, Any] | None = None
    _auth_error: str | None = None
    _error_detail: str | None = None

    def __init__(self) -> None:
        """Track codes already exchanged; SolarEdge codes are single-use."""
        super().__init__()
        self._codes_tried: set[str] = set()

    @property
    def logger(self) -> logging.Logger:
        """Return the flow logger."""
        return logging.getLogger(__name__)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SolarEdgeConfigEntry) -> SolarEdgeOptionsFlow:
        """Return the options flow."""
        return SolarEdgeOptionsFlow()

    def _authorize_link(self) -> str:
        """SolarEdge sign-in URL; ``external_id`` comes back on the redirect (no ``state``)."""
        if self._authorize_url is None:
            async_register_view(self.hass)
            external_id = secrets.token_urlsafe(16)
            pending_flows(self.hass)[external_id] = self.flow_id
            self._authorize_url = str(
                URL(OAUTH2_AUTHORIZE).with_query(
                    {
                        "client_id": getattr(self.flow_impl, "client_id", ""),
                        "scope": OAUTH2_SCOPES,
                        "access_duration": OAUTH2_ACCESS_DURATION,
                        "external_id": external_id,
                        "redirect_uri": callback_url(self.hass),
                    }
                )
            )
        return self._authorize_url

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Replace HA's generic OAuth step (SolarEdge never returns ``state``)."""
        return await self.async_step_sign_in()

    async def async_step_sign_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show where SolarEdge must redirect, then sign in automatically or manually."""
        return self.async_show_menu(
            step_id="sign_in",
            menu_options=["authorize", "paste"],
            description_placeholders={"callback_url": callback_url(self.hass)},
        )

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Open SolarEdge; our callback view resumes this step with the code."""
        if user_input is None:
            return self.async_external_step(step_id="authorize", url=self._authorize_link())
        self.external_data = user_input
        site = str(user_input.get("site_id", ""))
        if site.isdigit():
            self._site_from_redirect = int(site)
        next_step = "authorize_rejected" if "error" in user_input else "creation"
        return self.async_external_step_done(next_step_id=next_step)

    @callback
    def async_remove(self) -> None:
        """Forget this flow's pending sign-in."""
        flows = pending_flows(self.hass)
        for key in [k for k, v in flows.items() if v == self.flow_id]:
            flows.pop(key)

    async def async_step_creation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange the code from the callback; fall back to pasting on failure."""
        await self._async_exchange_code(self.external_data["code"])
        if self._token is not None:
            return await self.async_oauth_create_entry(
                {"auth_implementation": self.flow_impl.domain, "token": self._token}
            )
        return await self.async_step_paste()

    async def async_step_paste(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual sign-in: paste the address SolarEdge sent the tab to.

        Used when the SolarEdge app isn't set up to redirect to our callback, or
        after a refused code.
        """
        errors: dict[str, str] = {}
        if self._auth_error:
            errors["base"] = self._auth_error
        if user_input is not None:
            params = parse_redirect(user_input.get(CONF_REDIRECT_URL, ""))
            site = params.get("site_id", "")
            if site.isdigit():
                self._site_from_redirect = int(site)
            code = params.get("code")
            if "error" in params:
                self._auth_error = "authorize_rejected"
            elif code and code not in self._codes_tried:
                await self._async_exchange_code(code)
            if self._token is not None:
                return await self.async_oauth_create_entry(
                    {"auth_implementation": self.flow_impl.domain, "token": self._token}
                )
            errors["base"] = self._auth_error or "missing_code"

        return self.async_show_form(
            step_id="paste",
            data_schema=vol.Schema({vol.Optional(CONF_REDIRECT_URL): selector.TextSelector()}),
            description_placeholders={
                "authorize_url": self._authorize_link(),
                "error_detail": self._error_detail or "no details",
            },
            errors=errors,
        )

    async def _async_exchange_code(self, code: str) -> None:
        """Trade a code for tokens once; remember the outcome."""
        self._codes_tried.add(code)
        try:
            async with asyncio.timeout(30):
                token = await self.flow_impl.async_resolve_external_data(
                    {"code": code, "state": {"redirect_uri": None}}
                )
            token["expires_in"] = int(token["expires_in"])
        except (OAuth2TokenRequestError, ClientError, TimeoutError, KeyError, ValueError) as err:
            self.logger.warning("SolarEdge token exchange failed: %s", err)
            self._auth_error = "oauth_failed"
            self._error_detail = getattr(err, "message", None) or str(err) or type(err).__name__
            return
        token["expires_at"] = time.time() + token["expires_in"]
        self._token = token
        self._auth_error = None

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Store the token; reauth finishes here, setup continues to the site step."""
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if self._site_from_redirect and self._site_from_redirect != entry.data[CONF_SITE_ID]:
                return self.async_abort(reason="wrong_site")
            return self.async_update_reload_and_abort(entry, data_updates={"token": data["token"]})
        self._oauth_data = data
        if self._site_from_redirect:
            return await self.async_step_site({CONF_SITE_ID: self._site_from_redirect})
        return await self.async_step_site()

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the site ID (HA's OAuth callback drops SolarEdge's site_id)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            site_id = int(user_input[CONF_SITE_ID])
            token = self._oauth_data["token"]["access_token"]

            async def _token() -> str:
                return token

            client = SolarEdgeClient(async_get_clientsession(self.hass), _token)
            try:
                site = await client.get_site(site_id)
            except SolarEdgeNotFoundError, SolarEdgeForbiddenError:
                errors["base"] = "invalid_site"
            except SolarEdgeAuthError:
                errors["base"] = "invalid_auth"
            except SolarEdgeError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(str(site_id))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=site.name, data={**self._oauth_data, CONF_SITE_ID: site_id}
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SITE_ID): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="site",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth when the token or consent grant is no longer valid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()


class SolarEdgeOptionsFlow(OptionsFlowWithReload):
    """Credit budget options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MONTHLY_BUDGET,
                    default=options.get(CONF_MONTHLY_BUDGET, DEFAULT_MONTHLY_BUDGET),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=100, max=1_000_000, step=50, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_BILLING_DAY,
                    default=options.get(CONF_BILLING_DAY, DEFAULT_BILLING_DAY),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=31, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_DAY_SHARE,
                    default=options.get(CONF_DAY_SHARE, DEFAULT_DAY_SHARE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=50, max=95, step=5, unit_of_measurement="%")
                ),
                vol.Required(
                    CONF_ENERGY_INTERVAL,
                    default=options.get(CONF_ENERGY_INTERVAL, DEFAULT_ENERGY_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=24, step=1, unit_of_measurement="h")
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
