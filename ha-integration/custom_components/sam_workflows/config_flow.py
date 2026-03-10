"""Config flow for SAM Workflows."""

import voluptuous as vol

from homeassistant import config_entries

from . import DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("namespace", default="sam"): str,
        vol.Optional("timeout_seconds", default=30): vol.All(
            int, vol.Range(min=5, max=120)
        ),
    }
)


class SAMWorkflowsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SAM Workflows."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            return self.async_create_entry(title="SAM Workflows", data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)
