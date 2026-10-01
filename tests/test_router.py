"""Routing through ESPHome's real event handler."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.esphome.assist_satellite import EsphomeAssistSatellite
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.satellite_router.const import DOMAIN

from .conftest import make_satellite, sent

ROUTED = "reg-office"
TTS_OUTPUT = {
    "url": "/api/tts_proxy/abc.flac",
    "token": "abc.flac",
    "media_id": "x",
    "mime_type": "audio/flac",
}
ORIGINAL = EsphomeAssistSatellite.__dict__["on_pipeline_event"]


def _entry(**route) -> MockConfigEntry:
    data = {
        "satellite": ROUTED,
        "media_players": ["media_player.office_sonos"],
        "stt_script": "script.stt_listen",
        "tts_script": "script.tts_speak",
        **route,
    }
    return MockConfigEntry(
        domain=DOMAIN,
        title="Satellite Router",
        subentries_data=[
            ConfigSubentryData(
                data=data, subentry_type="satellite", title="Office Voice", unique_id=ROUTED
            )
        ],
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _run(sat, *events: tuple[PipelineEventType, dict | None]) -> None:
    for kind, data in events:
        sat.on_pipeline_event(PipelineEvent(kind, data))


CONVERSATION = (
    (PipelineEventType.RUN_START, {"pipeline": "x", "language": "en"}),
    (PipelineEventType.STT_START, {"engine": "stt.x", "metadata": {}}),
    (PipelineEventType.STT_END, {"stt_output": {"text": "what time is it"}}),
    (PipelineEventType.INTENT_PROGRESS, {"tts_start_streaming": True}),
    (
        PipelineEventType.TTS_START,
        {"engine": "tts.piper", "language": "en_US", "voice": "amy", "tts_input": "It is noon."},
    ),
    (PipelineEventType.TTS_END, {"tts_output": TTS_OUTPUT}),
    (PipelineEventType.RUN_END, None),
)


async def test_routed_conversation(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry())
    sat = make_satellite(hass, ROUTED)
    _run(sat, *CONVERSATION)
    await hass.async_block_till_done()

    # The satellite hears everything but the audio.
    assert sent(sat) == [
        ("VOICE_ASSISTANT_RUN_START", {}),
        ("VOICE_ASSISTANT_STT_START", {}),
        ("VOICE_ASSISTANT_STT_END", {"text": "what time is it"}),
        ("VOICE_ASSISTANT_TTS_START", {"text": "It is noon."}),
        ("VOICE_ASSISTANT_TTS_END", {}),
        ("VOICE_ASSISTANT_RUN_END", {}),
    ]
    assert calls == [
        (
            "script.stt_listen",
            {
                "media_player_volume_level": 0.3,
                "media_player_entity_id": ["media_player.office_sonos"],
            },
        ),
        (
            "script.tts_speak",
            {
                "media_player_volume_level": 0.3,
                "media_player_entity_id": ["media_player.office_sonos"],
                "entity_id": "tts.piper",
                "message": "It is noon.",
                "language": "en_US",
                "options": {"voice": "amy"},
            },
        ),
    ]


async def test_unrouted_satellite_untouched(hass: HomeAssistant, calls) -> None:
    await _setup(hass, _entry())
    sat = make_satellite(hass, "reg-other")
    _run(sat, *CONVERSATION)
    await hass.async_block_till_done()
    events = dict(sent(sat))
    assert events["VOICE_ASSISTANT_INTENT_PROGRESS"] == {"tts_start_streaming": "1"}
    assert events["VOICE_ASSISTANT_TTS_END"]["url"].endswith("/api/tts_proxy/abc.flac")
    assert calls == []


async def test_tts_speak_without_script(hass: HomeAssistant, calls) -> None:
    await _setup(hass, _entry(tts_script=None, stt_script=None))
    sat = make_satellite(hass, ROUTED)
    _run(sat, *CONVERSATION)
    await hass.async_block_till_done()
    assert calls == [
        (
            "tts.speak",
            {
                "entity_id": "tts.piper",
                "message": "It is noon.",
                "media_player_entity_id": ["media_player.office_sonos"],
                "language": "en_US",
                "options": {"voice": "amy"},
            },
        ),
    ]
    assert ("VOICE_ASSISTANT_TTS_END", {}) in sent(sat)


async def test_announcement_url_stripped(hass: HomeAssistant, calls) -> None:
    """A run that starts with TTS audio (e.g. start_conversation) doesn't play it on the satellite."""
    await _setup(hass, _entry())
    sat = make_satellite(hass, ROUTED)
    _run(sat, (PipelineEventType.RUN_START, {"pipeline": "x", "tts_output": TTS_OUTPUT}))
    assert sent(sat) == [("VOICE_ASSISTANT_RUN_START", {})]


async def test_route_changes_apply_without_reload(hass: HomeAssistant, calls) -> None:
    entry = _entry()
    await _setup(hass, entry)
    sub = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry, sub, data={**sub.data, "tts_script": None, "stt_script": None}
    )
    await hass.async_block_till_done()
    sat = make_satellite(hass, ROUTED)
    _run(sat, *CONVERSATION)
    await hass.async_block_till_done()
    assert [c[0] for c in calls] == ["tts.speak"]


async def test_unload_restores_esphome(hass: HomeAssistant, calls) -> None:
    entry = _entry()
    await _setup(hass, entry)
    assert EsphomeAssistSatellite.__dict__["on_pipeline_event"] is not ORIGINAL
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert EsphomeAssistSatellite.__dict__["on_pipeline_event"] is ORIGINAL
    sat = make_satellite(hass, ROUTED)
    _run(sat, *CONVERSATION)
    assert dict(sent(sat))["VOICE_ASSISTANT_TTS_END"]["url"]


async def test_router_error_never_breaks_satellite(hass: HomeAssistant, calls) -> None:
    await _setup(hass, _entry())
    sat = make_satellite(hass, ROUTED)
    with patch(
        "custom_components.satellite_router.router.Router.process", side_effect=RuntimeError("boom")
    ):
        _run(sat, *CONVERSATION)
    assert dict(sent(sat))["VOICE_ASSISTANT_TTS_END"]["url"]


async def test_patched_core_stays_inactive(hass: HomeAssistant, calls) -> None:
    with patch(
        "homeassistant.components.esphome.const.CONF_TTS_MEDIA_PLAYER_SCRIPT",
        "tts_media_player_script",
        create=True,
    ):
        await _setup(hass, _entry())
    assert EsphomeAssistSatellite.__dict__["on_pipeline_event"] is ORIGINAL
    assert ir.async_get(hass).async_get_issue(DOMAIN, "patched_core")


async def test_incompatible_esphome(hass: HomeAssistant, calls) -> None:
    def on_pipeline_event(self, event, extra):  # a future signature
        pass

    with patch.object(EsphomeAssistSatellite, "on_pipeline_event", on_pipeline_event):
        await _setup(hass, _entry())
        assert EsphomeAssistSatellite.__dict__["on_pipeline_event"] is on_pipeline_event
    assert ir.async_get(hass).async_get_issue(DOMAIN, "incompatible_esphome")
