"""Routing devices that run Assist pipelines themselves (e.g. a Companion app tablet)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.satellite_router.const import ACTIVE_DEVICE_STATE, DOMAIN
from custom_components.satellite_router.router import pipeline_run_class

RUN_CLS = pipeline_run_class()
ORIGINAL = RUN_CLS.__dict__["process_event"]
TTS_OUTPUT = {"url": "/api/tts_proxy/abc.mp3", "token": "abc.mp3", "mime_type": "audio/mpeg"}
STREAM_OUTPUT = {**TTS_OUTPUT, "stream_response": True}


@pytest.fixture
def tablet(hass: HomeAssistant) -> str:
    """A Companion app device in the Office area; returns its device ID."""
    app = MockConfigEntry(domain="mobile_app", title="Office Tablet")
    app.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Office")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=app.entry_id, identifiers={("mobile_app", "tablet")}, name="Office Tablet"
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    return device.id


def _entry(device_id: str, **route) -> MockConfigEntry:
    data = {
        "device": device_id,
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
                data=data, subentry_type="device", title="Office Tablet", unique_id=f"device:{device_id}"
            )
        ],
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def _run(hass: HomeAssistant, device_id: str | None, satellite_id: str | None = None):
    """A real core PipelineRun, wired just enough for process_event; returns (run, events seen)."""
    hass.data["assist_pipeline"] = SimpleNamespace(pipeline_debug={"p": {}})
    run = object.__new__(RUN_CLS)
    seen: list[PipelineEvent] = []
    run.hass = hass
    run.pipeline = SimpleNamespace(id="p")
    run.id = "r"
    run.event_callback = seen.append
    run._device_id = device_id
    run._satellite_id = satellite_id
    return run, seen


def _send(run, *events: tuple[PipelineEventType, dict | None]) -> None:
    for kind, data in events:
        run.process_event(PipelineEvent(kind, data))


def _conversation(text: str = "It is noon.", follow_up: bool = False):
    return (
        (PipelineEventType.RUN_START, {"pipeline": "p", "language": "en", "tts_output": STREAM_OUTPUT}),
        (PipelineEventType.STT_START, {"engine": "stt.x", "metadata": {}}),
        (PipelineEventType.STT_END, {"stt_output": {"text": "what time is it"}}),
        (PipelineEventType.INTENT_PROGRESS, {"tts_start_streaming": True}),
        (
            PipelineEventType.INTENT_END,
            {"intent_output": {"continue_conversation": follow_up, "response": {}}},
        ),
        (
            PipelineEventType.TTS_START,
            {"engine": "tts.piper", "language": "en_US", "voice": "amy", "tts_input": text},
        ),
        (PipelineEventType.TTS_END, {"tts_output": TTS_OUTPUT}),
        (PipelineEventType.RUN_END, None),
    )


def _by_type(seen: list[PipelineEvent]) -> dict[PipelineEventType, dict]:
    return {e.type: e.data or {} for e in seen}


async def test_routed_device_run(hass: HomeAssistant, calls, tablet) -> None:
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry(tablet))
    run, seen = _run(hass, tablet)
    _send(run, *_conversation()[:2])
    active = hass.states.get(ACTIVE_DEVICE_STATE)
    assert active.state == "office"
    assert active.attributes["device_id"] == tablet
    assert active.attributes["device"] == "Office Tablet"
    _send(run, *_conversation()[2:])
    await hass.async_block_till_done(wait_background_tasks=True)

    events = _by_type(seen)
    # The app would play the run-start URL at once: gone. The tts-end URL is
    # blanked, not removed (the app can't parse a missing one).
    assert "tts_output" not in events[PipelineEventType.RUN_START]
    assert events[PipelineEventType.TTS_END]["tts_output"] == {**TTS_OUTPUT, "url": ""}
    assert events[PipelineEventType.INTENT_PROGRESS]["tts_start_streaming"] is False
    assert calls == [
        (
            "script.stt_listen",
            {"media_player_entity_id": ["media_player.office_sonos"], "media_player_volume_level": 0.3},
        ),
        (
            "script.tts_speak",
            {
                "media_player_entity_id": ["media_player.office_sonos"],
                "media_player_volume_level": 0.3,
                "entity_id": "tts.piper",
                "message": "It is noon.",
                "language": "en_US",
                "options": {"voice": "amy"},
            },
        ),
    ]
    assert hass.states.get(ACTIVE_DEVICE_STATE).state == "idle"


async def test_unrouted_device_untouched(hass: HomeAssistant, calls, tablet) -> None:
    await _setup(hass, _entry(tablet))
    run, seen = _run(hass, "some-other-device")
    _send(run, *_conversation())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [(e.type, e.data) for e in seen] == [(k, d) for k, d in _conversation()]
    assert calls == []
    assert hass.states.get(ACTIVE_DEVICE_STATE) is None


async def test_satellite_runs_left_to_the_satellite_hook(hass: HomeAssistant, calls, tablet) -> None:
    # A run with a satellite is the satellite hook's business, even for a routed device.
    await _setup(hass, _entry(tablet))
    run, seen = _run(hass, tablet, satellite_id="assist_satellite.office_voice")
    _send(run, *_conversation())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _by_type(seen)[PipelineEventType.TTS_END]["tts_output"] == TTS_OUTPUT
    assert calls == []


async def test_run_without_device_untouched(hass: HomeAssistant, calls, tablet) -> None:
    await _setup(hass, _entry(tablet))
    run, seen = _run(hass, None)
    _send(run, *_conversation())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _by_type(seen)[PipelineEventType.RUN_START]["tts_output"] == STREAM_OUTPUT
    assert calls == []


async def test_follow_up_question_spoken_by_device(hass: HomeAssistant, calls, tablet) -> None:
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry(tablet))
    run, seen = _run(hass, tablet)
    _send(run, *_conversation("Which room?", follow_up=True))
    await hass.async_block_till_done(wait_background_tasks=True)

    # The device plays the question (its microphone reopens as soon as it has
    # nothing to play, and would hear the speakers); only the duck ran.
    assert _by_type(seen)[PipelineEventType.TTS_END]["tts_output"] == TTS_OUTPUT
    assert [c[0] for c in calls] == ["script.stt_listen"]

    # The answer: the speakers are still ducked, the original volume is restored after.
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.1})
    run2, seen2 = _run(hass, tablet)
    _send(run2, *_conversation("Done."))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _by_type(seen2)[PipelineEventType.TTS_END]["tts_output"]["url"] == ""
    assert calls[1] == (
        "script.stt_listen",
        {"media_player_entity_id": ["media_player.office_sonos"], "media_player_volume_level": 0.3},
    )
    assert calls[2][0] == "script.tts_speak"
    assert calls[2][1]["message"] == "Done."
    assert calls[2][1]["media_player_volume_level"] == 0.3


async def test_route_without_scripts_uses_tts_speak(hass: HomeAssistant, calls, tablet) -> None:
    await _setup(hass, _entry(tablet, stt_script=None, tts_script=None))
    run, seen = _run(hass, tablet)
    _send(run, *_conversation())
    await hass.async_block_till_done(wait_background_tasks=True)
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
        )
    ]
    assert _by_type(seen)[PipelineEventType.TTS_END]["tts_output"]["url"] == ""


async def test_unload_restores_pipeline_run(hass: HomeAssistant, calls, tablet) -> None:
    entry = _entry(tablet)
    await _setup(hass, entry)
    assert RUN_CLS.__dict__["process_event"] is not ORIGINAL
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert RUN_CLS.__dict__["process_event"] is ORIGINAL


async def test_router_error_never_breaks_the_run(hass: HomeAssistant, calls, tablet, monkeypatch) -> None:
    entry = _entry(tablet)
    await _setup(hass, entry)

    def boom(*_):
        raise RuntimeError("boom")

    monkeypatch.setattr(entry.runtime_data, "process_device", boom)
    run, seen = _run(hass, tablet)
    _send(run, *_conversation())
    assert len(seen) == len(_conversation())


async def test_voice_mute_switch(hass: HomeAssistant, calls, tablet) -> None:
    """The mute switch turns the app's wake word off and drops spoken requests."""
    from homeassistant.components.assist_pipeline.error import SpeechToTextError
    from homeassistant.core import ServiceCall

    app = hass.config_entries.async_entries("mobile_app")[0]
    hass.config_entries.async_update_entry(app, data={"device_name": "Office Tablet"})
    sent: list[dict] = []

    async def notify(call: ServiceCall) -> None:
        sent.append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_office_tablet", notify)
    await _setup(hass, _entry(tablet))
    entity = "switch.office_tablet_voice_mute"
    assert hass.states.get(entity).state == "off"

    await hass.services.async_call("switch", "turn_on", {"entity_id": entity}, blocking=True)
    assert hass.states.get(entity).state == "on"
    assert sent == [{"message": "command_wake_word_detection", "data": {"command": "turn_off"}}]
    run, seen = _run(hass, tablet)
    _send(run, *_conversation()[:1])
    with pytest.raises(SpeechToTextError) as err:
        _send(run, *_conversation()[1:2])
    assert err.value.code == "voice-muted"
    # Nothing past run-start reached the app, and the speakers weren't ducked.
    assert [e.type for e in seen] == [PipelineEventType.RUN_START]
    assert calls == []

    await hass.services.async_call("switch", "turn_off", {"entity_id": entity}, blocking=True)
    assert sent[-1] == {"message": "command_wake_word_detection", "data": {"command": "turn_on"}}
    run, seen = _run(hass, tablet)
    _send(run, *_conversation()[:2])
    assert PipelineEventType.STT_START in _by_type(seen)


async def test_voice_mute_without_notify(hass: HomeAssistant, tablet) -> None:
    """No notify action: the switch still mutes (requests dropped)."""
    from homeassistant.components.assist_pipeline.error import SpeechToTextError

    await _setup(hass, _entry(tablet))
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.office_tablet_voice_mute"}, blocking=True
    )
    run, _seen = _run(hass, tablet)
    with pytest.raises(SpeechToTextError):
        _send(run, *_conversation()[:2])
