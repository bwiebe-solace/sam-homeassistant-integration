"""SAM conversation entity — registers SAM as an HA voice/conversation agent."""

import asyncio
import json
import logging
import uuid

from homeassistant.components.conversation import (
    AssistantContent,
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
        """Forward the user's message to SAM and await the response."""
        namespace: str = self._entry_data["namespace"]
        timeout_seconds: int = self._entry_data["timeout_seconds"]
        pending: dict[str, asyncio.Future[str]] = self._entry_data[
            "pending_conversations"
        ]
        manager = self._entry_data.get("mqtt_manager")

        session_id = user_input.conversation_id or str(uuid.uuid4())
        future: asyncio.Future[str] = self.hass.loop.create_future()
        pending[session_id] = future

        if manager:
            await manager.publish(
                f"{namespace}/ha/conversation/request",
                json.dumps({"text": user_input.text, "session_id": session_id}),
                qos=1,
            )
        _LOGGER.debug(
            "Sent to SAM — session=%s text=%r", session_id, user_input.text[:100]
        )

        try:
            response_text = await asyncio.wait_for(
                asyncio.shield(future), timeout=float(timeout_seconds)
            )
        except asyncio.TimeoutError:
            pending.pop(session_id, None)
            _LOGGER.warning("SAM response timed out (session=%s)", session_id)
            response_text = "Sorry, SAM didn't respond in time. Please try again."

        # Update the chat log so HA records the assistant turn.
        chat_log.async_add_assistant_content_without_tools(
            AssistantContent(
                agent_id=user_input.agent_id,
                content=response_text,
            )
        )

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response_text)
        return ConversationResult(
            response=intent_response,
            conversation_id=session_id,
        )
