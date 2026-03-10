"""SAM STT provider — forwards audio streams to SAM's web UI gateway for transcription."""

import logging
from typing import AsyncIterable

import aiohttp

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SAM STT entity."""
    async_add_entities([SAMSpeechToTextEntity(hass, entry)])


class SAMSpeechToTextEntity(SpeechToTextEntity):
    """STT entity that forwards audio to SAM's /stt endpoint (Whisper or Azure)."""

    _attr_has_entity_name = True
    _attr_name = "SAM STT"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_stt"

    # HA's voice pipeline sends 16-bit PCM WAV at 16 kHz mono.
    # Declare exactly those capabilities so HA knows what to send us.

    @property
    def supported_languages(self) -> list[str]:
        return [
            "en", "fr", "de", "es", "pt", "it", "nl", "pl", "ja", "ko", "zh",
            "ru", "ar", "hi", "tr", "sv", "da", "fi", "nb", "uk",
        ]

    @property
    def supported_formats(self) -> list[AudioFormats]:
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        return [AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return [AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        return [AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Collect the audio stream and transcribe via SAM's /stt endpoint."""
        sam_url = self._entry.data["sam_url"].rstrip("/")
        session = async_get_clientsession(self.hass)

        audio_data = bytearray()
        async for chunk in stream:
            audio_data.extend(chunk)

        if not audio_data:
            _LOGGER.warning("SAM STT received empty audio stream")
            return SpeechResult("", SpeechResultState.ERROR)

        form = aiohttp.FormData()
        form.add_field(
            "file",
            bytes(audio_data),
            filename="audio.wav",
            content_type="audio/wav",
        )
        if metadata.language:
            form.add_field("language", metadata.language)

        try:
            async with session.post(
                f"{sam_url}/stt", data=form, timeout=_REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "SAM STT returned HTTP %s: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return SpeechResult("", SpeechResultState.ERROR)
                result = await resp.json()

            text = result.get("text", "").strip()
            if not text:
                return SpeechResult("", SpeechResultState.ERROR)
            return SpeechResult(text, SpeechResultState.SUCCESS)

        except Exception as exc:
            _LOGGER.error("SAM STT request failed: %s", exc)
            return SpeechResult("", SpeechResultState.ERROR)
