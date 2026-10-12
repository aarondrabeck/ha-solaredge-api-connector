"""Config flow for SolarEdge V2."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

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


class OAuth2FlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Authorize with SolarEdge, then pick the site the grant covers."""

    DOMAIN = DOMAIN
    VERSION = 1

    _oauth_data: dict[str, Any]

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

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Store the token; reauth finishes here, setup continues to the site step."""
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={"token": data["token"]}
            )
        self._oauth_data = data
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
