"""SAM Workflows — HA custom integration.

Bridges HomeAssistant to Solace Agent Mesh via MQTT:
  - Registers SAM as a conversation agent (voice assistant)
  - Provides a sam_workflows.trigger_workflow service so automations can
    fire arbitrary SAM workflows
"""

import asyncio
import json
import logging
from typing import Callable

import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

DOMAIN = "sam_workflows"
PLATFORMS = ["conversation"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SAM Workflows from a config entry."""
    namespace: str = entry.data["namespace"]
    timeout_seconds: int = entry.data.get("timeout_seconds", 30)

    # Futures keyed by session_id, resolved when SAM publishes a response.
    pending_conversations: dict[str, asyncio.Future[str]] = {}
    # Unsubscribe callables returned by mqtt.async_subscribe.
    unsubscribers: list[Callable] = []

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "namespace": namespace,
        "timeout_seconds": timeout_seconds,
        "pending_conversations": pending_conversations,
    }

    # --- MQTT subscriptions ---

    async def _handle_response(msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Non-JSON payload on response topic %s", msg.topic)
            return
        session_id = payload.get("session_id")
        text = payload.get("text", "").strip()
        future = pending_conversations.pop(session_id, None)
        if future and not future.done():
            future.set_result(text or "(no response)")

    async def _handle_error(msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Non-JSON payload on error topic %s", msg.topic)
            return
        session_id = payload.get("session_id")
        error_text = payload.get("error", "Unknown error from SAM.")
        future = pending_conversations.pop(session_id, None)
        if future and not future.done():
            # Resolve with a human-readable error rather than raising, so the
            # conversation entity can speak it back to the user.
            future.set_result(f"Sorry, SAM encountered an error: {error_text}")

    unsubscribers.append(
        await mqtt.async_subscribe(
            hass,
            f"{namespace}/ha/conversation/response/+",
            _handle_response,
        )
    )
    unsubscribers.append(
        await mqtt.async_subscribe(
            hass,
            f"{namespace}/ha/conversation/error/+",
            _handle_error,
        )
    )

    hass.data[DOMAIN][entry.entry_id]["unsubscribers"] = unsubscribers

    # --- Workflow trigger service ---

    async def _trigger_workflow(call: ServiceCall) -> None:
        workflow_name: str = call.data["workflow_name"]
        data: dict = call.data.get("data", {})
        topic = f"{namespace}/ha/workflows/{workflow_name}"
        await mqtt.async_publish(hass, topic, json.dumps(data), qos=1)
        _LOGGER.info("Triggered SAM workflow '%s'", workflow_name)

    hass.services.async_register(
        DOMAIN,
        "trigger_workflow",
        _trigger_workflow,
        schema=vol.Schema(
            {
                vol.Required("workflow_name"): str,
                vol.Optional("data", default={}): dict,
            }
        ),
    )

    # --- Set up conversation platform ---
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        for unsub in entry_data.get("unsubscribers", []):
            unsub()
        hass.services.async_remove(DOMAIN, "trigger_workflow")
    return unloaded
