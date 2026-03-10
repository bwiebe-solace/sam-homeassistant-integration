"""SAM TTS provider — forwards text-to-speech requests to SAM's web UI gateway."""

import logging
from typing import Any

import aiohttp

from homeassistant.components.tts import TextToSpeechEntity, TtsAudioType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SAM TTS entity."""
    async_add_entities([SAMTextToSpeechEntity(hass, entry)])


class SAMTextToSpeechEntity(TextToSpeechEntity):
    """TTS entity that forwards synthesis requests to SAM's /tts endpoint."""

    _attr_has_entity_name = True
    _attr_name = "SAM TTS"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_tts"

    @property
    def supported_languages(self) -> list[str]:
        # Whisper (SAM's STT) and Gemini/Azure (SAM's TTS) support many languages.
        # Declare a broad set; the pipeline passes the configured language at runtime.
        return [
            "en", "fr", "de", "es", "pt", "it", "nl", "pl", "ja", "ko", "zh",
            "ru", "ar", "hi", "tr", "sv", "da", "fi", "nb", "uk",
        ]

    @property
    def default_language(self) -> str:
        return "en"

    @property
    def supported_options(self) -> list[str]:
        return ["voice"]

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        """Request synthesised audio from SAM's /tts endpoint."""
        sam_url = self._entry.data["sam_url"].rstrip("/")
        session = async_get_clientsession(self.hass)

        body: dict[str, Any] = {"text": message}
        if voice := options.get("voice"):
            body["voice"] = voice

        try:
            async with session.post(
                f"{sam_url}/tts", json=body, timeout=_REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "SAM TTS returned HTTP %s: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return None, None
                return "mp3", await resp.read()
        except Exception as exc:
            _LOGGER.error("SAM TTS request failed: %s", exc)
            return None, None
