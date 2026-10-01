"""Fixtures for Satellite Router tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.components.esphome.assist_satellite import EsphomeAssistSatellite
from homeassistant.core import HomeAssistant, ServiceCall

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture(autouse=True)
def enable_integration(enable_custom_integrations):
    """Allow Home Assistant to load the custom integration under test."""


@pytest.fixture(autouse=True)
async def no_esphome_setup(hass: HomeAssistant):
    """Satellite Router depends on esphome and assist_pipeline; don't really set them up."""
    hass.config.components.update({"esphome", "assist_pipeline"})
    hass.config.internal_url = "http://ha.local:8123"


@pytest.fixture
def calls(hass: HomeAssistant) -> list[tuple[str, dict]]:
    """Calls to script.stt_listen, script.tts_speak and tts.speak."""
    seen: list[tuple[str, dict]] = []

    async def handler(call: ServiceCall) -> None:
        seen.append((f"{call.domain}.{call.service}", dict(call.data)))

    for domain, service in (("script", "stt_listen"), ("script", "tts_speak"), ("tts", "speak")):
        hass.services.async_register(domain, service, handler)
    return seen


def make_satellite(hass: HomeAssistant, registry_id: str) -> EsphomeAssistSatellite:
    """A real ESPHome satellite entity, wired just enough to run on_pipeline_event."""
    sat = object.__new__(EsphomeAssistSatellite)
    sat.hass = hass
    sat.cli = MagicMock()
    sat.registry_entry = SimpleNamespace(id=registry_id)
    entry_data = MagicMock()
    entry_data.device_info.voice_assistant_feature_flags_compat.return_value = 0
    sat._entry_data = entry_data
    sat._has_multi_channel_audio = False
    sat._active_audio_channel = 0
    sat._tts_streaming_task = None
    sat.config_entry = MagicMock()
    return sat


def sent(sat: EsphomeAssistSatellite) -> list[tuple[str, dict]]:
    """(event type name, data) the satellite was sent."""
    return [(c.args[0].name, c.args[1]) for c in sat.cli.send_voice_assistant_event.call_args_list]
