"""
HA MQTT Gateway Adapter for Solace Agent Mesh.

Subscribes to HomeAssistant MQTT topics on the Solace broker and routes
conversation requests and workflow triggers to the SAM orchestrator.

Inbound topics (HA → SAM):
  {namespace}/ha/conversation/request
    payload: {"text": "...", "session_id": "..."}
  {namespace}/ha/workflows/{name}
    payload: any JSON (forwarded as task context)

Outbound topics (SAM → HA):
  {namespace}/ha/conversation/response/{session_id}
    payload: {"text": "...", "session_id": "..."}
  {namespace}/ha/conversation/error/{session_id}
    payload: {"error": "...", "session_id": "..."}
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


class HAMqttAdapterConfig(BaseModel):
    mqtt_host: str = Field("", description="Solace broker MQTT hostname. Leave empty to disable the gateway.")
    mqtt_port: int = Field(8883, description="Solace broker MQTT port (8883 for TLS).")
    mqtt_username: str = Field("", description="MQTT username.")
    mqtt_password: str = Field("", description="MQTT password.")
    mqtt_tls: bool = Field(True, description="Use TLS for the MQTT connection.")
    target_agent: str = Field(
        "OrchestratorAgent", description="SAM agent to route all requests to."
    )


class HAMqttAdapter(GatewayAdapter):
    """
    Gateway adapter that bridges HomeAssistant MQTT topics to the SAM agent mesh.

    Each inbound MQTT message becomes a SAM task routed to the orchestrator.
    Responses are published back on a per-session reply topic so the HA
    conversation entity can await them.
    """

    ConfigModel = HAMqttAdapterConfig

    def __init__(self):
        self._context: Optional[GatewayContext] = None
        self._mqtt_client: Optional[aiomqtt.Client] = None
        self._listener_task: Optional[asyncio.Task] = None
        # task_id → accumulated response text chunks
        self._response_buffers: Dict[str, List[str]] = {}

    async def init(self, context: GatewayContext) -> None:
        self._context = context
        if not context.adapter_config.mqtt_host:
            log.info(
                "HA MQTT gateway disabled — set SOLACE_MQTT_HOST to enable it"
            )
            return
        self._listener_task = asyncio.create_task(self._run_mqtt_listener())
        log.info("HA MQTT adapter initialised (namespace=%s)", context.namespace)

    async def cleanup(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        log.info("HA MQTT adapter stopped")

    # ------------------------------------------------------------------
    # MQTT listener loop (reconnects on transient errors)
    # ------------------------------------------------------------------

    async def _run_mqtt_listener(self) -> None:
        cfg: HAMqttAdapterConfig = self._context.adapter_config
        ns = self._context.namespace

        tls_context = ssl.create_default_context() if cfg.mqtt_tls else None
        conversation_topic = f"{ns}/ha/conversation/request"
        workflow_topic = f"{ns}/ha/workflows/#"

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
                    await client.subscribe(conversation_topic, qos=1)
                    await client.subscribe(workflow_topic, qos=1)
                    log.info(
                        "MQTT subscribed — conversation: %s  workflows: %s",
                        conversation_topic,
                        workflow_topic,
                    )
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
        ns = self._context.namespace

        try:
            payload = json.loads(message.payload)
        except (ValueError, TypeError):
            log.warning("Non-JSON payload on topic %s, ignoring", topic)
            return

        if topic == f"{ns}/ha/conversation/request":
            await self._handle_conversation(payload)
        elif topic.startswith(f"{ns}/ha/workflows/"):
            workflow_name = topic[len(f"{ns}/ha/workflows/"):]
            await self._handle_workflow(workflow_name, payload)

    async def _handle_conversation(self, payload: Dict[str, Any]) -> None:
        text = payload.get("text", "").strip()
        session_id = payload.get("session_id")
        if not text:
            log.warning("Conversation request has empty text, ignoring")
            return
        log.info("Conversation request session=%s text=%r", session_id, text[:100])
        await self._context.handle_external_input(
            {"type": "conversation", "text": text, "session_id": session_id}
        )

    async def _handle_workflow(
        self, workflow_name: str, payload: Dict[str, Any]
    ) -> None:
        params_str = json.dumps(payload) if payload else "no parameters"
        text = f"Execute workflow '{workflow_name}' with the following input: {params_str}"
        log.info("Workflow trigger: %s payload=%s", workflow_name, params_str[:200])
        await self._context.handle_external_input(
            {"type": "workflow", "workflow_name": workflow_name, "text": text}
        )

    # ------------------------------------------------------------------
    # GatewayAdapter interface — inbound (platform → SAM)
    # ------------------------------------------------------------------

    async def prepare_task(
        self,
        external_input: Dict[str, Any],
        endpoint_context: Optional[Dict[str, Any]] = None,
    ) -> SamTask:
        cfg: HAMqttAdapterConfig = self._context.adapter_config
        ns = self._context.namespace

        session_id = external_input.get("session_id")
        text = external_input["text"]
        input_type = external_input.get("type", "conversation")

        response_topic = (
            f"{ns}/ha/conversation/response/{session_id}" if session_id else None
        )
        error_topic = (
            f"{ns}/ha/conversation/error/{session_id}" if session_id else None
        )
        stream_topic = (
            f"{ns}/ha/conversation/stream/{session_id}" if session_id else None
        )

        return SamTask(
            parts=[SamTextPart(text=text)],
            session_id=session_id,
            target_agent=cfg.target_agent,
            is_streaming=True,
            platform_context={
                "input_type": input_type,
                "response_topic": response_topic,
                "error_topic": error_topic,
                "stream_topic": stream_topic,
            },
        )

    # ------------------------------------------------------------------
    # GatewayAdapter interface — outbound (SAM → platform)
    # ------------------------------------------------------------------

    async def handle_text_chunk(self, text: str, context: ResponseContext) -> None:
        self._response_buffers.setdefault(context.task_id, []).append(text)
        stream_topic = context.platform_context.get("stream_topic")
        if stream_topic and text:
            await self._publish(stream_topic, {"chunk": text, "done": False})

    async def handle_task_complete(self, context: ResponseContext) -> None:
        parts = self._response_buffers.pop(context.task_id, [])
        full_text = "".join(parts).strip()

        stream_topic = context.platform_context.get("stream_topic")
        if stream_topic:
            await self._publish(stream_topic, {"chunk": "", "done": True})

        response_topic = context.platform_context.get("response_topic")
        if response_topic and full_text:
            # Still publish full response for backward-compatible clients.
            await self._publish(
                response_topic,
                {"text": full_text, "session_id": context.session_id},
            )

    async def handle_error(self, error: SamError, context: ResponseContext) -> None:
        self._response_buffers.pop(context.task_id, None)
        error_topic = context.platform_context.get("error_topic")
        if error_topic:
            await self._publish(
                error_topic,
                {"error": error.message, "session_id": context.session_id},
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
