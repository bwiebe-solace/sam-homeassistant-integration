"""SAM conversation entity — registers SAM as an HA voice/conversation agent."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator

from homeassistant.components.conversation import (
    AssistantContentDeltaDict,
    ChatLog,
    ConversationEntity,
    ConversationInput,
    ConversationResult,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SAM conversation entity."""
    async_add_entities([SAMConversationEntity(hass, entry)])


class SAMConversationEntity(ConversationEntity):
    """Conversation entity that routes queries to SAM via MQTT."""

    _attr_has_entity_name = True
    _attr_name = "SAM"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_conversation"

    @property
    def supported_languages(self) -> list[str] | str:
        return "*"

    @property
    def _entry_data(self) -> dict:
        return self.hass.data[DOMAIN][self._entry.entry_id]

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Forward the user's message to SAM and stream the response back."""
        namespace: str = self._entry_data["namespace"]
        timeout_seconds: int = self._entry_data["timeout_seconds"]
        pending: dict[str, asyncio.Queue[str | None]] = self._entry_data[
            "pending_conversations"
        ]
        manager = self._entry_data.get("mqtt_manager")

        session_id = user_input.conversation_id or str(uuid.uuid4())
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        pending[session_id] = queue

        if manager:
            await manager.publish(
                f"{namespace}/ha/conversation/request",
                json.dumps({"text": user_input.text, "session_id": session_id}),
                qos=1,
            )
        _LOGGER.debug(
            "Sent to SAM — session=%s text=%r", session_id, user_input.text[:100]
        )

        full_text_parts: list[str] = []

        async def _chunk_gen() -> AsyncGenerator[AssistantContentDeltaDict, None]:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                full_text_parts.append(chunk)
                yield AssistantContentDeltaDict(role="assistant", content=chunk)

        async def _drain() -> None:
            async for _ in chat_log.async_add_delta_content_stream(
                user_input.agent_id, _chunk_gen()
            ):
                pass

        try:
            await asyncio.wait_for(_drain(), timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            pending.pop(session_id, None)
            _LOGGER.warning("SAM response timed out (session=%s)", session_id)
            response_text = "Sorry, SAM didn't respond in time. Please try again."
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_speech(response_text)
            return ConversationResult(
                response=intent_response,
                conversation_id=session_id,
            )

        response_text = "".join(full_text_parts).strip() or "(no response)"
        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response_text)
        return ConversationResult(
            response=intent_response,
            conversation_id=session_id,
        )
