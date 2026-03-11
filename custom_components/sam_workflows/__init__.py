"""SAM Workflows — HA custom integration.

Bridges HomeAssistant to Solace Agent Mesh via a managed MQTT connection.
The component connects directly to the Solace broker using aiomqtt — no
dependency on HA's built-in MQTT integration is required.

  - Registers SAM as a conversation agent (voice assistant)
  - Auto-discovers SAM workflows and registers them as native HA services
  - Provides a sam_workflows.trigger_workflow fallback service
"""

import asyncio
import json
import logging
import re
import ssl
from typing import Any

import aiomqtt
import voluptuous as vol

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


class _MQTTManager:
    """Manages the component's direct MQTT connection to the Solace broker."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        namespace: str,
        pending_conversations: dict[str, asyncio.Future[str]],
        registered_workflows: dict[str, str],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._namespace = namespace
        self._pending_conversations = pending_conversations
        self._registered_workflows = registered_workflows
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        self._task = self._hass.async_create_background_task(
            self._run(), "sam_workflows_mqtt"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        for future in self._pending_conversations.values():
            if not future.done():
                future.set_result("SAM connection closed.")
        self._pending_conversations.clear()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._client = None

    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        if self._client is None:
            _LOGGER.warning("Cannot publish to %s — MQTT not connected", topic)
            return
        await self._client.publish(topic, payload=payload, qos=qos)

    async def _run(self) -> None:
        data = self._entry.data
        tls_context = ssl.create_default_context() if data.get("mqtt_tls", True) else None
        reconnect_interval = 5
        client_id = f"ha-sam-{self._entry.entry_id[:8]}"

        while not self._stop_event.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=data["mqtt_host"],
                    port=data.get("mqtt_port", 8883),
                    username=data.get("mqtt_username"),
                    password=data.get("mqtt_password"),
                    tls_context=tls_context,
                    identifier=client_id,
                ) as client:
                    self._client = client
                    ns = self._namespace
                    await client.subscribe(f"{ns}/ha/conversation/response/+")
                    await client.subscribe(f"{ns}/ha/conversation/error/+")
                    await client.subscribe(f"{ns}/a2a/v1/discovery/agentcards")
                    _LOGGER.info(
                        "SAM Workflows MQTT connected to %s:%s",
                        data["mqtt_host"],
                        data.get("mqtt_port", 8883),
                    )
                    async for message in client.messages:
                        if self._stop_event.is_set():
                            break
                        topic = str(message.topic)
                        if "conversation/response" in topic:
                            self._on_response(message)
                        elif "conversation/error" in topic:
                            self._on_error(message)
                        elif "discovery/agentcards" in topic:
                            await self._on_agent_card(message)

            except aiomqtt.MqttError as exc:
                self._client = None
                if not self._stop_event.is_set():
                    _LOGGER.warning(
                        "SAM Workflows MQTT error: %s — reconnecting in %ds",
                        exc,
                        reconnect_interval,
                    )
                    await asyncio.sleep(reconnect_interval)
            except asyncio.CancelledError:
                break

        self._client = None

    def _on_response(self, message: aiomqtt.Message) -> None:
        try:
            payload = json.loads(message.payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Non-JSON payload on response topic %s", message.topic)
            return
        session_id = payload.get("session_id")
        text = payload.get("text", "").strip()
        future = self._pending_conversations.pop(session_id, None)
        if future and not future.done():
            future.set_result(text or "(no response)")

    def _on_error(self, message: aiomqtt.Message) -> None:
        try:
            payload = json.loads(message.payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Non-JSON payload on error topic %s", message.topic)
            return
        session_id = payload.get("session_id")
        error_text = payload.get("error", "Unknown error from SAM.")
        future = self._pending_conversations.pop(session_id, None)
        if future and not future.done():
            future.set_result(f"Sorry, SAM encountered an error: {error_text}")

    async def _on_agent_card(self, message: aiomqtt.Message) -> None:
        try:
            card = json.loads(message.payload)
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
        if service_name in self._registered_workflows:
            return

        display_name: str = card.get("displayName") or workflow_name
        input_schema = _get_ext(extensions, _EXT_SCHEMAS).get("input_schema")
        svc_schema = _json_schema_to_vol(input_schema)

        _LOGGER.info(
            "Discovered SAM workflow '%s' → sam_workflows.%s",
            display_name,
            service_name,
        )

        namespace = self._namespace
        self_ref = self

        async def _invoke(call: ServiceCall, _wf: str = workflow_name) -> None:
            topic = f"{namespace}/ha/workflows/{_wf}"
            await self_ref.publish(topic, json.dumps(dict(call.data)), qos=1)
            _LOGGER.info("Invoked SAM workflow '%s'", _wf)

        self._hass.services.async_register(DOMAIN, service_name, _invoke, schema=svc_schema)
        self._registered_workflows[service_name] = workflow_name


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate config entries from older versions."""
    if config_entry.version < 2:
        _LOGGER.warning(
            "SAM Workflows config entry (version %s) requires broker credentials "
            "added in version 2. Please delete and re-add the integration.",
            config_entry.version,
        )
        return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SAM Workflows from a config entry."""
    namespace: str = entry.data["namespace"]
    timeout_seconds: int = entry.data.get("timeout_seconds", 30)

    pending_conversations: dict[str, asyncio.Future[str]] = {}
    registered_workflows: dict[str, str] = {}

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "namespace": namespace,
        "timeout_seconds": timeout_seconds,
        "pending_conversations": pending_conversations,
        "registered_workflows": registered_workflows,
    }

    manager = _MQTTManager(hass, entry, namespace, pending_conversations, registered_workflows)
    manager.start()
    hass.data[DOMAIN][entry.entry_id]["mqtt_manager"] = manager

    async def _trigger_workflow(call: ServiceCall) -> None:
        workflow_name: str = call.data["workflow_name"]
        data: dict = call.data.get("data", {})
        topic = f"{namespace}/ha/workflows/{workflow_name}"
        await manager.publish(topic, json.dumps(data), qos=1)
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
        manager: _MQTTManager | None = entry_data.get("mqtt_manager")
        if manager:
            await manager.stop()
        for service_name in entry_data.get("registered_workflows", {}):
            hass.services.async_remove(DOMAIN, service_name)
        hass.services.async_remove(DOMAIN, "trigger_workflow")
    return unloaded
