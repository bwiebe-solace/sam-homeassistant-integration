"""SAM Workflows — HA custom integration.

Bridges HomeAssistant to Solace Agent Mesh via MQTT:
  - Registers SAM as a conversation agent (voice assistant)
  - Auto-discovers SAM workflows and registers them as native HA services
  - Provides a sam_workflows.trigger_workflow fallback service
"""

import asyncio
import json
import logging
import re
from typing import Any, Callable

import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

DOMAIN = "sam_workflows"
_ALWAYS_PLATFORMS = ["conversation"]
_SPEECH_PLATFORMS = ["tts", "stt"]

# SAM agent card extension URIs
_EXT_AGENT_TYPE = "https://solace.com/a2a/extensions/agent-type"
_EXT_SCHEMAS = "https://solace.com/a2a/extensions/sam/schemas"


def _get_ext(extensions: list, uri: str) -> dict:
    """Return the params dict for a given extension URI, or {}."""
    for ext in extensions:
        if ext.get("uri") == uri:
            return ext.get("params") or {}
    return {}


def _to_service_name(name: str) -> str:
    """Convert a SAM workflow name (CamelCase or spaced) to snake_case."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^\w]", "", s)
    return s.lower()


def _json_schema_to_vol(schema: dict | None) -> vol.Schema:
    """Convert a simple JSON Schema object definition to a voluptuous schema.

    Only handles flat object schemas. Falls back to ALLOW_EXTRA for anything
    complex so the service always accepts input rather than rejecting it.
    """
    if not schema or schema.get("type") != "object":
        return vol.Schema({}, extra=vol.ALLOW_EXTRA)

    _TYPE_MAP: dict[str, Any] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    required = set(schema.get("required", []))
    vol_fields: dict = {}
    for key, prop in schema.get("properties", {}).items():
        prop_type = _TYPE_MAP.get(prop.get("type", "string"), object)
        default = prop.get("default")
        if key in required:
            vol_fields[vol.Required(key)] = prop_type
        elif default is not None:
            vol_fields[vol.Optional(key, default=default)] = prop_type
        else:
            vol_fields[vol.Optional(key)] = prop_type

    return vol.Schema(vol_fields, extra=vol.ALLOW_EXTRA)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SAM Workflows from a config entry."""
    namespace: str = entry.data["namespace"]
    timeout_seconds: int = entry.data.get("timeout_seconds", 30)

    # Futures keyed by session_id, resolved when SAM publishes a response.
    pending_conversations: dict[str, asyncio.Future[str]] = {}
    # Unsubscribe callables returned by mqtt.async_subscribe.
    unsubscribers: list[Callable] = []
    # service_name (snake_case) → original SAM workflow name
    registered_workflows: dict[str, str] = {}

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "namespace": namespace,
        "timeout_seconds": timeout_seconds,
        "pending_conversations": pending_conversations,
        "registered_workflows": registered_workflows,
    }

    # --- Conversation response/error subscriptions ---

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

    # --- Workflow auto-discovery ---
    # SAM publishes an AgentCard to this topic on every heartbeat interval.
    # Cards are camelCase JSON (Pydantic serialize_by_alias=True).
    # Extensions live under capabilities.extensions, not at the card root.

    async def _handle_agent_card(msg: mqtt.ReceiveMessage) -> None:
        try:
            card = json.loads(msg.payload)
        except (ValueError, TypeError):
            return

        extensions: list = (card.get("capabilities") or {}).get("extensions") or []
        agent_type = _get_ext(extensions, _EXT_AGENT_TYPE).get("type")
        if agent_type != "workflow":
            return

        workflow_name: str = card.get("name", "")
        if not workflow_name:
            return

        service_name = _to_service_name(workflow_name)

        if service_name in registered_workflows:
            return  # already registered; heartbeat re-announcements are ignored

        display_name: str = card.get("displayName") or workflow_name
        input_schema = _get_ext(extensions, _EXT_SCHEMAS).get("input_schema")
        svc_schema = _json_schema_to_vol(input_schema)

        _LOGGER.info(
            "Discovered SAM workflow '%s' → sam_workflows.%s",
            display_name,
            service_name,
        )

        async def _invoke(call: ServiceCall, _wf: str = workflow_name) -> None:
            topic = f"{namespace}/ha/workflows/{_wf}"
            await mqtt.async_publish(hass, topic, json.dumps(dict(call.data)), qos=1)
            _LOGGER.info("Invoked SAM workflow '%s'", _wf)

        hass.services.async_register(DOMAIN, service_name, _invoke, schema=svc_schema)
        registered_workflows[service_name] = workflow_name

    unsubscribers.append(
        await mqtt.async_subscribe(
            hass,
            f"{namespace}/a2a/v1/discovery/agentcards",
            _handle_agent_card,
        )
    )

    hass.data[DOMAIN][entry.entry_id]["unsubscribers"] = unsubscribers

    # --- Generic fallback: trigger any workflow by name ---

    async def _trigger_workflow(call: ServiceCall) -> None:
        workflow_name: str = call.data["workflow_name"]
        data: dict = call.data.get("data", {})
        topic = f"{namespace}/ha/workflows/{workflow_name}"
        await mqtt.async_publish(hass, topic, json.dumps(data), qos=1)
        _LOGGER.info("Triggered SAM workflow '%s' via trigger_workflow", workflow_name)

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

    # --- Platform setup ---
    platforms = list(_ALWAYS_PLATFORMS)
    if entry.data.get("sam_url"):
        platforms.extend(_SPEECH_PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    hass.data[DOMAIN][entry.entry_id]["platforms"] = platforms

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    platforms = hass.data[DOMAIN].get(entry.entry_id, {}).get(
        "platforms", _ALWAYS_PLATFORMS
    )
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unloaded:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        for unsub in entry_data.get("unsubscribers", []):
            unsub()
        for service_name in entry_data.get("registered_workflows", {}):
            hass.services.async_remove(DOMAIN, service_name)
        hass.services.async_remove(DOMAIN, "trigger_workflow")
    return unloaded
