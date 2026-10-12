"""Config flow for SolarEdge V2."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
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
from homeassistant.helpers.network import NoURLAvailableError
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
    OAUTH2_SCOPES,
)

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
    _callback_code: str | None = None
    _site_from_redirect: int | None = None

    @property
    def logger(self) -> logging.Logger:
        """Return the flow logger."""
        return logging.getLogger(__name__)

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Request both scopes and the longest consent grant."""
        return {"scope": OAUTH2_SCOPES, "access_duration": OAUTH2_ACCESS_DURATION}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SolarEdgeConfigEntry) -> SolarEdgeOptionsFlow:
        """Return the options flow."""
        return SolarEdgeOptionsFlow()

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in at SolarEdge, then finish from the callback or a pasted redirect URL.

        HA's callback needs ``state`` echoed back and drops ``site_id``; SolarEdge's
        redirect may omit the former. A form with the sign-in link and an optional
        "paste the address you landed on" field works in every case.
        """
        errors: dict[str, str] = {}
        if user_input is not None and "state" in user_input:
            # HA's OAuth callback view resumed us; keep the code until Submit.
            self._callback_code = user_input.get("code")
            if "error" in user_input:
                errors["base"] = "authorize_rejected"
        elif user_input is not None:
            params = parse_redirect(user_input.get(CONF_REDIRECT_URL, ""))
            code = params.get("code") or self._callback_code
            if "error" in params:
                errors["base"] = "authorize_rejected"
            elif not code:
                errors["base"] = "missing_code"
            else:
                site = params.get("site_id", "")
                self._site_from_redirect = int(site) if site.isdigit() else None
                try:
                    return await self._async_exchange_code(code)
                except (
                    OAuth2TokenRequestError,
                    ClientError,
                    TimeoutError,
                    KeyError,
                    ValueError,
                ) as err:
                    self.logger.warning("SolarEdge token exchange failed: %s", err)
                    self._callback_code = None
                    errors["base"] = "oauth_failed"

        if self._authorize_url is None:
            try:
                async with asyncio.timeout(30):
                    self._authorize_url = await self.async_generate_authorize_url()
            except TimeoutError:
                return self.async_abort(reason="authorize_url_timeout")
            except NoURLAvailableError:
                return self.async_abort(reason="no_url_available")

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema(
                {vol.Optional(CONF_REDIRECT_URL): selector.TextSelector()},
                # The OAuth callback view submits state/code to this step.
                extra=vol.ALLOW_EXTRA,
            ),
            description_placeholders={"authorize_url": self._authorize_url},
            errors=errors,
        )

    async def _async_exchange_code(self, code: str) -> ConfigFlowResult:
        redirect_uri = getattr(self.flow_impl, "redirect_uri", None)
        async with asyncio.timeout(30):
            token = await self.flow_impl.async_resolve_external_data(
                {"code": code, "state": {"redirect_uri": redirect_uri}}
            )
        token["expires_in"] = int(token["expires_in"])
        token["expires_at"] = time.time() + token["expires_in"]
        return await self.async_oauth_create_entry(
            {"auth_implementation": self.flow_impl.domain, "token": token}
        )

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

        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SITE_ID): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    )
                }
            ),
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
