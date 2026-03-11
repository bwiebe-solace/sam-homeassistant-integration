"""Config flow for SAM Workflows."""

import logging
import ssl

import aiomqtt
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("mqtt_host"): str,
        vol.Optional("mqtt_port", default=8883): vol.All(
            int, vol.Range(min=1, max=65535)
        ),
        vol.Required("mqtt_username"): str,
        vol.Required("mqtt_password"): str,
        vol.Optional("mqtt_tls", default=True): bool,
        vol.Required("namespace", default="sam"): str,
        vol.Optional("timeout_seconds", default=30): vol.All(
            int, vol.Range(min=5, max=120)
        ),
        vol.Optional("sam_url", default=""): str,
    }
)


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
