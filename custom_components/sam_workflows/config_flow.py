"""Config flow for SAM Workflows."""

import logging
import ssl

import aiomqtt
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _build_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("mqtt_host", default=defaults.get("mqtt_host", "")): str,
            vol.Optional("mqtt_port", default=defaults.get("mqtt_port", 8883)): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            vol.Required("mqtt_username", default=defaults.get("mqtt_username", "")): str,
            vol.Required("mqtt_password", default=defaults.get("mqtt_password", "")): str,
            vol.Optional("mqtt_tls", default=defaults.get("mqtt_tls", True)): bool,
            vol.Required("namespace", default=defaults.get("namespace", "sam")): str,
            vol.Optional(
                "timeout_seconds", default=defaults.get("timeout_seconds", 30)
            ): vol.All(int, vol.Range(min=5, max=300)),
            vol.Optional("sam_url", default=defaults.get("sam_url", "")): str,
        }
    )


STEP_USER_SCHEMA = _build_schema({})


async def _test_connection(data: dict) -> None:
    """Raise CannotConnect if the broker is not reachable with the given credentials."""
    tls_context = ssl.create_default_context() if data.get("mqtt_tls", True) else None
    try:
        async with aiomqtt.Client(
            hostname=data["mqtt_host"],
            port=data.get("mqtt_port", 8883),
            username=data.get("mqtt_username"),
            password=data.get("mqtt_password"),
            tls_context=tls_context,
        ):
            pass
    except (aiomqtt.MqttError, OSError) as exc:
        raise CannotConnect(str(exc)) from exc


class CannotConnect(Exception):
    pass


class SAMWorkflowsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SAM Workflows."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(config_entry):
        return SAMWorkflowsOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _test_connection(user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="SAM Workflows", data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )


class SAMWorkflowsOptionsFlow(config_entries.OptionsFlow):
    """Handle SAM Workflows options (reconfigure after initial setup)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        current = {**self._config_entry.data, **self._config_entry.options}
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _test_connection(user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(current),
            errors=errors,
        )
