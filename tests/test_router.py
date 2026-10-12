"""Routing through ESPHome's real event handler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.assist_satellite import AssistSatelliteAnnouncement
from homeassistant.components.esphome.assist_satellite import EsphomeAssistSatellite
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceCall
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
ORIGINAL_START = EsphomeAssistSatellite.__dict__["async_start_conversation"]
ORIGINAL_ANNOUNCE = EsphomeAssistSatellite.__dict__["async_announce"]
CHIME = "http://ha.local:8123/api/assist_satellite/static/preannounce.mp3"
PIPELINE = SimpleNamespace(tts_engine="tts.piper", tts_language="en_US", tts_voice="amy")


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
    await hass.async_block_till_done(wait_background_tasks=True)


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
    await hass.async_block_till_done(wait_background_tasks=True)

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
    await hass.async_block_till_done(wait_background_tasks=True)
    events = dict(sent(sat))
    assert events["VOICE_ASSISTANT_INTENT_PROGRESS"] == {"tts_start_streaming": "1"}
    assert events["VOICE_ASSISTANT_TTS_END"]["url"].endswith("/api/tts_proxy/abc.flac")
    assert calls == []


async def test_tts_speak_without_script(hass: HomeAssistant, calls) -> None:
    await _setup(hass, _entry(tts_script=None, stt_script=None))
    sat = make_satellite(hass, ROUTED)
    _run(sat, *CONVERSATION)
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
    await hass.async_block_till_done(wait_background_tasks=True)
    sat = make_satellite(hass, ROUTED)
    _run(sat, *CONVERSATION)
    await hass.async_block_till_done(wait_background_tasks=True)
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


async def test_tts_speak_fallback_with_listening_script(hass: HomeAssistant, calls) -> None:
    """The listening script's volume isn't passed to tts.speak, which would reject it."""
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry(tts_script=None))
    sat = make_satellite(hass, ROUTED)
    _run(sat, *CONVERSATION)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls[1] == (
        "tts.speak",
        {
            "entity_id": "tts.piper",
            "message": "It is noon.",
            "media_player_entity_id": ["media_player.office_sonos"],
            "language": "en_US",
            "options": {"voice": "amy"},
        },
    )


async def test_response_script_without_media_players(hass: HomeAssistant, calls) -> None:
    """A response script alone still gets the response (the satellite is silenced)."""
    await _setup(hass, _entry(media_players=[], stt_script=None))
    sat = make_satellite(hass, ROUTED)
    _run(sat, *CONVERSATION)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == [
        (
            "script.tts_speak",
            {
                "entity_id": "tts.piper",
                "message": "It is noon.",
                "language": "en_US",
                "options": {"voice": "amy"},
            },
        ),
    ]
    assert ("VOICE_ASSISTANT_TTS_END", {}) in sent(sat)


async def test_silenced_response_returns_satellite_to_idle(hass: HomeAssistant, calls) -> None:
    """The device never plays a routed response, so the hook reports it finished at RUN_END."""
    await _setup(hass, _entry())
    sat = make_satellite(hass, ROUTED)
    sat.tts_response_finished = MagicMock()
    _run(sat, *CONVERSATION[:-1])
    sat.tts_response_finished.assert_not_called()  # not before the run ends
    _run(sat, CONVERSATION[-1])
    sat.tts_response_finished.assert_called_once_with()
    # Only once per run: a later run without a response doesn't repeat it.
    _run(sat, (PipelineEventType.RUN_START, {"pipeline": "x"}), (PipelineEventType.RUN_END, None))
    sat.tts_response_finished.assert_called_once_with()


async def test_unrouted_satellite_reports_its_own_finish(hass: HomeAssistant, calls) -> None:
    """An unrouted satellite plays its response and reports the finish itself."""
    await _setup(hass, _entry())
    sat = make_satellite(hass, "reg-other")
    sat.tts_response_finished = MagicMock()
    _run(sat, *CONVERSATION)
    sat.tts_response_finished.assert_not_called()


async def test_run_without_response_audio_not_finished(hass: HomeAssistant, calls) -> None:
    """No audio was removed (e.g. an empty sentence-trigger reply), so nothing to finish."""
    await _setup(hass, _entry())
    sat = make_satellite(hass, ROUTED)
    sat.tts_response_finished = MagicMock()
    _run(
        sat,
        (PipelineEventType.RUN_START, {"pipeline": "x"}),
        (PipelineEventType.TTS_END, {"tts_output": None}),
        (PipelineEventType.RUN_END, None),
    )
    sat.tts_response_finished.assert_not_called()


async def test_finish_error_never_breaks_satellite(hass: HomeAssistant, calls) -> None:
    await _setup(hass, _entry())
    sat = make_satellite(hass, ROUTED)
    sat.tts_response_finished = MagicMock(side_effect=RuntimeError("boom"))
    _run(sat, *CONVERSATION)  # must not raise
    assert ("VOICE_ASSISTANT_RUN_END", {}) in sent(sat)


def _announcement(message: str = "Want the porch light on?", **kw) -> AssistSatelliteAnnouncement:
    return AssistSatelliteAnnouncement(
        message=message,
        media_id=kw.pop("media_id", "http://ha.local:8123/api/tts_proxy/q.flac"),
        original_media_id="media-source://tts/x",
        tts_token="q.flac",
        media_id_source=kw.pop("media_id_source", "tts"),
        preannounce_media_id=kw.pop("preannounce_media_id", CHIME),
    )


def _speaking_satellite(hass: HomeAssistant, registry_id: str = ROUTED):
    sat = make_satellite(hass, registry_id)
    sat.cli.send_voice_assistant_announcement_await_response = AsyncMock()
    sat._resolve_pipeline = MagicMock(return_value=None)
    return sat


def _device_announcements(sat) -> list[tuple[str, str, bool, str]]:
    """(media_id, text, start_conversation, preannounce) the device was asked to play."""
    return [
        (c.args[0], c.args[2], c.kwargs["start_conversation"], c.kwargs["preannounce_media_id"])
        for c in sat.cli.send_voice_assistant_announcement_await_response.call_args_list
    ]


SPOKEN_QUESTION = {
    "media_player_entity_id": ["media_player.office_sonos"],
    "media_player_volume_level": 0.3,
    "message": "Want the porch light on?",
    "entity_id": "tts.piper",
    "language": "en_US",
    "options": {"voice": "amy"},
}


@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_start_conversation_speaks_on_media_players(_, hass: HomeAssistant, calls) -> None:
    """The opening message plays on the Sonos; the satellite plays the chime, then listens."""
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry())
    _ducking_listen_script(hass, calls)
    sat = _speaking_satellite(hass)
    await sat.async_start_conversation(_announcement())
    # Ducked first, as for a listen; the question leaves the speakers ducked.
    assert calls == [
        (
            "script.stt_listen",
            {"media_player_entity_id": ["media_player.office_sonos"], "media_player_volume_level": 0.3},
        ),
        ("script.tts_speak", {**SPOKEN_QUESTION, "media_player_volume_level": 0.15}),
    ]
    assert _device_announcements(sat) == [(CHIME, "Want the porch light on?", True, "")]

    # The answer's listen and the last response get the original volume back.
    _run(sat, *CONVERSATION)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls[2][1]["media_player_volume_level"] == 0.3
    assert calls[3][1]["media_player_volume_level"] == 0.3


@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_start_conversation_restores_volume_if_satellite_fails(_, hass, calls) -> None:
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry())
    _ducking_listen_script(hass, calls)
    sat = _speaking_satellite(hass)
    sat.cli.send_voice_assistant_announcement_await_response.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError):
        await sat.async_start_conversation(_announcement())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls[-1] == (
        "script.tts_speak",
        {"media_player_entity_id": ["media_player.office_sonos"], "media_player_volume_level": 0.3},
    )


def _ducking_listen_script(hass: HomeAssistant, calls) -> None:
    """A listening script that ducks the speakers, like the real one."""

    async def listen(call: ServiceCall) -> None:
        calls.append(("script.stt_listen", dict(call.data)))
        hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.15})

    hass.services.async_register("script", "stt_listen", listen)


@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_start_conversation_without_preannounce_still_cues(_, hass, calls) -> None:
    """The device only listens after playing something, so the chime is used anyway."""
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    await sat.async_start_conversation(_announcement(preannounce_media_id=None))
    assert _device_announcements(sat) == [(CHIME, "Want the porch light on?", True, "")]


@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_start_conversation_unrouted(_, hass: HomeAssistant, calls) -> None:
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass, "reg-other")
    await sat.async_start_conversation(_announcement())
    assert calls == []
    assert _device_announcements(sat) == [
        ("http://ha.local:8123/api/tts_proxy/q.flac", "Want the porch light on?", True, CHIME)
    ]


@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_start_conversation_falls_back_when_playback_fails(_, hass, calls) -> None:
    """If the media players can't play it, the satellite asks the question itself."""
    hass.services.async_remove("script", "tts_speak")
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    await sat.async_start_conversation(_announcement())
    assert _device_announcements(sat) == [
        ("http://ha.local:8123/api/tts_proxy/q.flac", "Want the porch light on?", True, CHIME)
    ]


@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_announcements_stay_on_satellite_by_default(_, hass, calls) -> None:
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    await sat.async_announce(_announcement("Dinner is ready."))
    assert calls == []
    assert _device_announcements(sat) == [
        ("http://ha.local:8123/api/tts_proxy/q.flac", "Dinner is ready.", False, CHIME)
    ]


@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_announcements_routed_when_enabled(_, hass: HomeAssistant, calls) -> None:
    await _setup(hass, _entry(announcements=True, stt_script=None, tts_script=None))
    sat = _speaking_satellite(hass)
    await sat.async_announce(_announcement("Dinner is ready."))
    assert calls == [
        (
            "tts.speak",
            {
                "message": "Dinner is ready.",
                "entity_id": "tts.piper",
                "language": "en_US",
                "options": {"voice": "amy"},
                "media_player_entity_id": ["media_player.office_sonos"],
            },
        )
    ]
    assert _device_announcements(sat) == []


@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_media_announcements_stay_on_satellite(_, hass: HomeAssistant, calls) -> None:
    """A sound or URL (no spoken message to re-synthesize) plays on the satellite."""
    await _setup(hass, _entry(announcements=True))
    sat = _speaking_satellite(hass)
    await sat.async_announce(
        _announcement("", media_id="http://x/doorbell.mp3", media_id_source="url")
    )
    assert calls == []
    assert _device_announcements(sat) == [("http://x/doorbell.mp3", "", False, CHIME)]


def _intent_end(follow_up: bool) -> tuple[PipelineEventType, dict]:
    return (
        PipelineEventType.INTENT_END,
        {
            "intent_output": {
                "conversation_id": "c1",
                "continue_conversation": follow_up,
                "response": {"speech": {"plain": {"speech": "It is noon."}}},
            }
        },
    )


async def test_follow_up_reopens_microphone_after_response(hass: HomeAssistant, calls) -> None:
    """A response that asks something plays on the Sonos, then the satellite listens again."""
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    order: list[str] = []
    sat.cli.send_voice_assistant_announcement_await_response.side_effect = (
        lambda *a, **k: order.append(f"listen {len(calls)} played")
    )
    _run(sat, *CONVERSATION[:4], _intent_end(True), *CONVERSATION[4:])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [c[0] for c in calls] == ["script.stt_listen", "script.tts_speak"]
    assert _device_announcements(sat) == [(CHIME, "", True, "")]
    assert order == ["listen 2 played"]  # only once the response finished playing


async def test_no_follow_up_without_continue(hass: HomeAssistant, calls) -> None:
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    _run(sat, *CONVERSATION[:4], _intent_end(False), *CONVERSATION[4:])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _device_announcements(sat) == []


async def test_follow_up_skipped_when_playback_fails(hass: HomeAssistant, calls) -> None:
    hass.services.async_remove("script", "tts_speak")
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    _run(sat, *CONVERSATION[:4], _intent_end(True), *CONVERSATION[4:])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _device_announcements(sat) == []


async def test_unload_restores_announcement_methods(hass: HomeAssistant, calls) -> None:
    entry = _entry()
    await _setup(hass, entry)
    assert EsphomeAssistSatellite.__dict__["async_start_conversation"] is not ORIGINAL_START
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert EsphomeAssistSatellite.__dict__["async_start_conversation"] is ORIGINAL_START
    assert EsphomeAssistSatellite.__dict__["async_announce"] is ORIGINAL_ANNOUNCE


async def test_speakers_stay_ducked_through_follow_up(hass: HomeAssistant, calls) -> None:
    """The question leaves the speakers ducked; the last response restores the original volume."""
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    _run(sat, *CONVERSATION[:3])
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.15})  # ducked
    _run(sat, CONVERSATION[3], _intent_end(True), *CONVERSATION[4:])
    await hass.async_block_till_done(wait_background_tasks=True)
    # The question: leave the speakers at the ducked volume.
    assert calls[1][0] == "script.tts_speak"
    assert calls[1][1]["media_player_volume_level"] == 0.15

    # The follow-up listen and the last response carry the original volume.
    _run(sat, *CONVERSATION[:4], _intent_end(False), *CONVERSATION[4:])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls[2] == (
        "script.stt_listen",
        {"media_player_volume_level": 0.3, "media_player_entity_id": ["media_player.office_sonos"]},
    )
    assert calls[3][0] == "script.tts_speak"
    assert calls[3][1]["media_player_volume_level"] == 0.3


async def test_volume_restored_when_follow_up_fails(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    sat.cli.send_voice_assistant_announcement_await_response.side_effect = RuntimeError("offline")
    _run(sat, *CONVERSATION[:3])
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.15})
    _run(sat, CONVERSATION[3], _intent_end(True), *CONVERSATION[4:])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls[-1] == (
        "script.tts_speak",
        {"media_player_entity_id": ["media_player.office_sonos"], "media_player_volume_level": 0.3},
    )


async def test_stale_kept_volume_ignored(hass: HomeAssistant, calls) -> None:
    """A follow-up that never started doesn't leak its volume into a later conversation."""
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.3})
    await _setup(hass, _entry())
    sat = _speaking_satellite(hass)
    _run(sat, *CONVERSATION[:3])
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.15})
    _run(sat, CONVERSATION[3], _intent_end(True), *CONVERSATION[4:])
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.6})
    with patch.object(hass.loop, "time", return_value=hass.loop.time() + 3600):
        _run(sat, *CONVERSATION)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls[-2][1]["media_player_volume_level"] == 0.6


def _lagging_listen_script(hass: HomeAssistant, calls, lag: float = 0.3) -> None:
    """A listening script that ducks, with the player reporting it a moment later (Sonos)."""

    async def listen(call: ServiceCall) -> None:
        calls.append(("script.stt_listen", dict(call.data)))
        hass.loop.call_later(
            lag,
            hass.states.async_set,
            "media_player.office_sonos",
            "playing",
            {"volume_level": 0.15},
        )

    hass.services.async_register("script", "stt_listen", listen)


@patch("custom_components.satellite_router.router.VOLUME_SETTLE", 2)
@patch("custom_components.satellite_router.router.async_get_pipeline", return_value=PIPELINE)
async def test_start_conversation_waits_for_reported_duck(_, hass, calls) -> None:
    """The question is told the ducked volume even when the player reports it late."""
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.5})
    await _setup(hass, _entry())
    _lagging_listen_script(hass, calls)
    sat = _speaking_satellite(hass)
    await sat.async_start_conversation(_announcement())
    assert calls[1][0] == "script.tts_speak"
    assert calls[1][1]["media_player_volume_level"] == 0.15


@patch("custom_components.satellite_router.router.VOLUME_SETTLE", 2)
async def test_restore_waits_for_duck_when_run_ends_at_once(hass: HomeAssistant, calls) -> None:
    """A run that fails straight after listening starts restores only after the duck shows."""
    hass.states.async_set("media_player.office_sonos", "playing", {"volume_level": 0.5})
    await _setup(hass, _entry())
    _lagging_listen_script(hass, calls)
    seen: list[float] = []

    async def speak(call: ServiceCall) -> None:
        seen.append(hass.states.get("media_player.office_sonos").attributes["volume_level"])
        calls.append(("script.tts_speak", dict(call.data)))

    hass.services.async_register("script", "tts_speak", speak)
    sat = _speaking_satellite(hass)
    _run(
        sat,
        (PipelineEventType.RUN_START, {"pipeline": "x"}),
        (PipelineEventType.STT_START, {"engine": "stt.x", "metadata": {}}),
        (PipelineEventType.ERROR, {"code": "stt-stream-failed", "message": "failed"}),
        (PipelineEventType.RUN_END, None),
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [c[0] for c in calls] == ["script.stt_listen", "script.tts_speak"]
    assert calls[1][1]["media_player_volume_level"] == 0.5  # restore to the original
    assert seen == [0.15]  # and only once the duck had shown
