"""
HA Events Gateway Adapter for Solace Agent Mesh.

Subscribes to HomeAssistant event MQTT topics and routes each inbound
event as an independent task to the SAM orchestrator.

Inbound topics (HA → SAM):
  Configurable via subscribe_topics (default: home/events/#)
  payload: JSON object with optional fields such as event_type, entity_id,
           state, old_state, unit, context

Outbound topics (SAM → HA):
  {response_topic_prefix}/{reply_topic}   (only when response_topic_prefix is set)
    payload: {"text": "...", "topic": "..."}
"""

import asyncio
import json
import logging
import ssl
from typing import Any, Dict, List, Optional

import aiomqtt
from pydantic import BaseModel, Field

from solace_agent_mesh.gateway.adapter.base import GatewayAdapter
from solace_agent_mesh.gateway.adapter.types import (
    GatewayContext,
    ResponseContext,
    SamError,
    SamTask,
    SamTextPart,
)

log = logging.getLogger(__name__)


class HAEventsAdapterConfig(BaseModel):
    mqtt_host: str = Field(..., description="Solace broker MQTT hostname.")
    mqtt_port: int = Field(8883, description="Solace broker MQTT port (8883 for TLS).")
    mqtt_username: str = Field(..., description="MQTT username.")
    mqtt_password: str = Field(..., description="MQTT password.")
    mqtt_tls: bool = Field(True, description="Use TLS for the MQTT connection.")
    subscribe_topics: List[str] = Field(
        default=["home/events/#"],
        description="List of MQTT topic filters to subscribe to (wildcard syntax).",
    )
    target_agent: str = Field(
        "OrchestratorAgent", description="SAM agent to route all events to."
    )
    response_topic_prefix: str = Field(
        "",
        description="If non-empty, publish SAM responses to {prefix}/{reply_topic}.",
    )


class HAEventsAdapter(GatewayAdapter):
    """
    Gateway adapter that bridges HomeAssistant event MQTT topics to the SAM agent mesh.

    Each inbound MQTT message becomes an independent SAM task routed to the
    configured target agent. Optionally publishes SAM responses back to an
    MQTT topic when response_topic_prefix is set.
    """

    ConfigModel = HAEventsAdapterConfig

    def __init__(self):
        self._context: Optional[GatewayContext] = None
        self._mqtt_client: Optional[aiomqtt.Client] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._response_buffers: Dict[str, List[str]] = {}

    async def init(self, context: GatewayContext) -> None:
        self._context = context
        self._listener_task = asyncio.create_task(self._run_mqtt_listener())
        log.info("HA Events adapter initialised (namespace=%s)", context.namespace)

    async def cleanup(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        log.info("HA Events adapter stopped")

    # ------------------------------------------------------------------
    # MQTT listener loop (reconnects on transient errors)
    # ------------------------------------------------------------------

    async def _run_mqtt_listener(self) -> None:
        cfg: HAEventsAdapterConfig = self._context.adapter_config

        tls_context = ssl.create_default_context() if cfg.mqtt_tls else None

        while True:
            try:
                async with aiomqtt.Client(
                    hostname=cfg.mqtt_host,
                    port=cfg.mqtt_port,
                    username=cfg.mqtt_username,
                    password=cfg.mqtt_password,
                    tls_context=tls_context,
                ) as client:
                    self._mqtt_client = client
                    for topic_filter in cfg.subscribe_topics:
                        await client.subscribe(topic_filter, qos=1)
                        log.info("MQTT subscribed to %s", topic_filter)
                    async for message in client.messages:
                        await self._dispatch(message)
            except aiomqtt.MqttError as exc:
                log.error("MQTT connection lost: %s — reconnecting in 5 s", exc)
                self._mqtt_client = None
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                return

    async def _dispatch(self, message: aiomqtt.Message) -> None:
        topic = str(message.topic)

        try:
            payload = json.loads(message.payload)
        except (ValueError, TypeError):
            log.warning("Non-JSON payload on topic %s, ignoring", topic)
            return

        log.info("HA event received on topic %s", topic)
        await self._context.handle_external_input(
            {"type": "ha_event", "topic": topic, "payload": payload}
        )

    # ------------------------------------------------------------------
    # GatewayAdapter interface — inbound (platform → SAM)
    # ------------------------------------------------------------------

    async def prepare_task(
        self,
        external_input: Dict[str, Any],
        endpoint_context: Optional[Dict[str, Any]] = None,
    ) -> SamTask:
        cfg: HAEventsAdapterConfig = self._context.adapter_config

        topic: str = external_input["topic"]
        payload: Dict[str, Any] = external_input.get("payload", {})

        json_payload = json.dumps(payload)
        task_text = f"HomeAssistant event on topic '{topic}': {json_payload}"

        reply_topic = topic.split("/", 1)[-1] if "/" in topic else topic

        return SamTask(
            parts=[SamTextPart(text=task_text)],
            target_agent=cfg.target_agent,
            is_streaming=True,
            platform_context={
                "reply_topic": reply_topic,
                "source_topic": topic,
            },
        )

    # ------------------------------------------------------------------
    # GatewayAdapter interface — outbound (SAM → platform)
    # ------------------------------------------------------------------

    async def handle_text_chunk(self, text: str, context: ResponseContext) -> None:
        self._response_buffers.setdefault(context.task_id, []).append(text)

    async def handle_task_complete(self, context: ResponseContext) -> None:
        parts = self._response_buffers.pop(context.task_id, [])
        full_text = "".join(parts).strip()

        cfg: HAEventsAdapterConfig = self._context.adapter_config
        if not cfg.response_topic_prefix:
            return

        reply_topic = context.platform_context.get("reply_topic")
        if not reply_topic:
            return

        if not full_text:
            log.warning("Task %s completed with empty response", context.task_id)
            return

        publish_topic = f"{cfg.response_topic_prefix}/{reply_topic}"
        await self._publish(
            publish_topic,
            {"text": full_text, "topic": context.platform_context.get("source_topic")},
        )

    async def handle_error(self, error: SamError, context: ResponseContext) -> None:
        self._response_buffers.pop(context.task_id, None)
        cfg: HAEventsAdapterConfig = self._context.adapter_config
        if not cfg.response_topic_prefix:
            return

        reply_topic = context.platform_context.get("reply_topic")
        if reply_topic:
            publish_topic = f"{cfg.response_topic_prefix}/{reply_topic}"
            await self._publish(
                publish_topic,
                {"error": error.message, "topic": context.platform_context.get("source_topic")},
            )

    async def _publish(self, topic: str, data: Dict[str, Any]) -> None:
        if not self._mqtt_client:
            log.error("Cannot publish to %s — MQTT client not connected", topic)
            return
        try:
            await self._mqtt_client.publish(topic, json.dumps(data), qos=1)
            log.info("Published to %s", topic)
        except Exception as exc:
            log.error("Failed to publish to %s: %s", topic, exc)
