"""Routes ESPHome voice satellites' speech to other media players.

ESPHome's satellite entity turns Assist pipeline events into messages for the
device in ``EsphomeAssistSatellite.on_pipeline_event``. This module wraps that
method. For a routed satellite it

* runs a script when speech-to-text starts (e.g. to duck the room's speakers),
* hands the response (text, TTS engine, language, voice) to a script, or to
  ``tts.speak``, for the room's media players, and
* removes the TTS audio from the events passed on, so the satellite itself
  stays silent (ESPHome then sends it the same events, minus the audio URL).

Events are only ever rewritten, never handled in ESPHome's place, so its own
code keeps doing everything else. Unrouted satellites are untouched.

One side effect needs undoing: the satellite entity enters ``responding`` on
TTS_START and only returns to ``idle`` when the device reports it finished
playing the response. A silenced device never plays it, so it never reports,
and the entity would sit in ``responding`` until its next run. For runs whose
audio was removed, the hook calls ``tts_response_finished()`` itself once
RUN_END has been handled.

Conversations need the device to play something, too: it only reopens its
microphone (for ``continue_conversation`` or ``start_conversation``) after it
finishes playing audio. So for those the response is played on the media
players and awaited, and then the satellite is sent a short cue (the
pre-announcement chime) as an announcement that starts a conversation. This
wraps ESPHome's ``async_start_conversation`` (and ``async_announce``, for
routes that route announcements) the same way.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from typing import Any

from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    async_get_pipeline,
)
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnnouncement,
    AssistSatelliteEntity,
)
from homeassistant.components.assist_satellite.const import PREANNOUNCE_URL
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

_MARK = "__satellite_router_original__"
_TTS_SPEAK_FIELDS = ("entity_id", "message", "media_player_entity_id", "language", "options")
_ANNOUNCE_METHODS = ("async_announce", "async_start_conversation")
# A follow-up listen starts within seconds of its question having played; a
# kept volume older than this belongs to a conversation that never continued.
FOLLOW_UP_WINDOW = 120


@dataclass(frozen=True, slots=True)
class Route:
    """Where one satellite's speech goes."""

    media_players: tuple[str, ...]
    stt_script: str | None
    tts_script: str | None
    announcements: bool = False

    @property
    def external_output(self) -> bool:
        """Whether responses play somewhere other than the satellite."""
        return bool(self.tts_script or self.media_players)


class Router:
    """Holds the routes and each satellite's response in progress."""

    def __init__(
        self,
        hass: HomeAssistant,
        run: Callable[[str, dict[str, Any]], None],
        call: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        spawn: Callable[[Coroutine[Any, Any, None], str], None] | None = None,
    ) -> None:
        """Set up.

        ``run(action, data)`` calls an action without waiting for it,
        ``call(action, data)`` calls one and waits until it has finished, and
        ``spawn(coro, name)`` runs a coroutine in the background.
        """
        self.hass = hass
        self._run = run
        self._call = call
        self._spawn = spawn
        self.routes: dict[str, Route] = {}  # by entity registry ID
        self._pending: dict[str, dict[str, Any]] = {}
        self._silenced: set[str] = set()  # runs whose response audio was removed
        self._follow_up: dict[str, Any] = {}  # satellites to reopen after the response
        # The media players' volume before a conversation's first listen, kept
        # across its follow-ups so the last response restores it.
        self._volume_before: dict[str, tuple[Any, float]] = {}  # (volume, when kept)

    @callback
    def process(
        self, registry_id: str | None, event: PipelineEvent, satellite: Any = None
    ) -> PipelineEvent:
        """Act on an event for a satellite; return the event ESPHome should see."""
        route = self.routes.get(registry_id) if registry_id else None
        if route is None:
            return event
        data = event.data or {}

        if event.type is PipelineEventType.STT_START:
            if route.stt_script:
                pending: dict[str, Any] = {}
                if route.media_players and (player := self.hass.states.get(route.media_players[0])):
                    pending["media_player_volume_level"] = self._take_volume_before(
                        registry_id, player.attributes.get("volume_level")
                    )
                    pending["media_player_entity_id"] = list(route.media_players)
                self._pending[registry_id] = pending
                self._run(route.stt_script, dict(pending))

        elif event.type is PipelineEventType.INTENT_PROGRESS:
            if route.external_output and data.get("tts_start_streaming"):
                # The satellite won't play the response, so it needn't prepare to stream it.
                return replace(event, data={**data, "tts_start_streaming": False})

        elif event.type is PipelineEventType.INTENT_END:
            output = data.get("intent_output") or {}
            if (
                route.external_output
                and output.get("continue_conversation")
                and satellite is not None
                and self._call is not None
                and self._spawn is not None
            ):
                self._follow_up[registry_id] = satellite

        elif event.type is PipelineEventType.TTS_START:
            # The core patch only collected this with media players set, so a
            # response script alone silenced the satellite and played nothing.
            if route.external_output:
                pending = self._pending.setdefault(registry_id, {})
                pending.update({"entity_id": data.get("engine"), "message": data.get("tts_input")})
                if route.media_players:
                    pending["media_player_entity_id"] = list(route.media_players)
                if data.get("language"):
                    pending["language"] = data["language"]
                if data.get("voice"):
                    pending["options"] = {"voice": data["voice"]}

        elif event.type is PipelineEventType.TTS_END:
            self._speak(registry_id, route)
            if route.external_output and data.get("tts_output"):
                self._silenced.add(registry_id)
                return replace(event, data={**data, "tts_output": None})

        elif event.type is PipelineEventType.RUN_START:
            self._follow_up.pop(registry_id, None)
            if route.external_output and data.get("tts_output"):
                return replace(event, data={k: v for k, v in data.items() if k != "tts_output"})

        elif event.type is PipelineEventType.RUN_END:
            self._speak(registry_id, route)
            self._follow_up.pop(registry_id, None)

        return event

    @callback
    def take_silenced(self, registry_id: str | None, event: PipelineEvent) -> bool:
        """Whether this RUN_END ends a run whose response audio was removed."""
        if event.type is not PipelineEventType.RUN_END or registry_id not in self._silenced:
            return False
        self._silenced.discard(registry_id)
        return True

    def _speak(self, registry_id: str, route: Route) -> None:
        """Hand a collected response to the TTS script, or to tts.speak."""
        pending = self._pending.get(registry_id)
        if not pending or (request := _request(route, pending)) is None:
            return
        del self._pending[registry_id]
        if (satellite := self._follow_up.pop(registry_id, None)) is not None:
            assert self._spawn is not None
            action, data = request
            if "media_player_volume_level" in data and route.media_players:
                # The conversation isn't over: ask the script to leave the speakers
                # as they are (ducked), and restore the volume after the last response.
                self._volume_before[registry_id] = (
                    data["media_player_volume_level"],
                    self.hass.loop.time(),
                )
                player = self.hass.states.get(route.media_players[0])
                data = {
                    **data,
                    "media_player_volume_level": player.attributes.get("volume_level")
                    if player
                    else data["media_player_volume_level"],
                }
            self._spawn(
                self._speak_then_listen(satellite, registry_id, route, action, data),
                "satellite_router follow-up",
            )
        else:
            self._run(*request)

    async def _speak_then_listen(
        self, satellite: Any, registry_id: str, route: Route, action: str, data: dict[str, Any]
    ) -> None:
        """Play a response that asks something, then reopen the satellite's microphone."""
        assert self._call is not None
        try:
            await self._call(action, data)
        except Exception:
            _LOGGER.exception("Satellite Router couldn't play the response; not listening again")
            self._restore_volume(registry_id, route)
            return
        if registry_id in self._volume_before:  # the window starts once the question has played
            self._volume_before[registry_id] = (
                self._volume_before[registry_id][0],
                self.hass.loop.time(),
            )
        try:
            # The cue isn't a TTS announcement, so the wrapped method passes it straight on.
            await satellite.async_start_conversation(_cue(self.hass, None, ""))
        except Exception:
            _LOGGER.exception("Satellite Router couldn't reopen the satellite's microphone")
            self._restore_volume(registry_id, route)

    def _take_volume_before(self, registry_id: str, current: Any) -> Any:
        """The volume to restore after this listen: the conversation's, if one is going on."""
        kept = self._volume_before.pop(registry_id, None)
        if kept is None or self.hass.loop.time() - kept[1] > FOLLOW_UP_WINDOW:
            return current
        return kept[0]

    def _restore_volume(self, registry_id: str, route: Route) -> None:
        """No follow-up after all: have the response script restore the volume."""
        if registry_id not in self._volume_before:
            return
        volume, _ = self._volume_before.pop(registry_id)
        if route.tts_script and route.media_players:
            self._run(
                route.tts_script,
                {
                    "media_player_entity_id": list(route.media_players),
                    "media_player_volume_level": volume,
                },
            )

    async def announce(
        self,
        satellite: Any,
        registry_id: str | None,
        announcement: AssistSatelliteAnnouncement,
        conversation: bool,
    ) -> AssistSatelliteAnnouncement | None:
        """Play an announcement on the media players.

        Returns what the satellite should still be sent: the announcement
        unchanged when it isn't routed, the listening cue for a conversation,
        or None when nothing is left for the satellite to do.
        """
        route = self.routes.get(registry_id) if registry_id else None
        if (
            route is None
            or self._call is None
            or not route.external_output
            or not (conversation or route.announcements)
            # Media other than a spoken message (a sound, a URL) stays on the satellite.
            or announcement.media_id_source != "tts"
            or not announcement.message
        ):
            return announcement
        pending: dict[str, Any] = {"message": announcement.message}
        pipeline = async_get_pipeline(self.hass, satellite._resolve_pipeline())
        pending["entity_id"] = pipeline.tts_engine
        if pipeline.tts_language:
            pending["language"] = pipeline.tts_language
        if pipeline.tts_voice:
            pending["options"] = {"voice": pipeline.tts_voice}
        if route.media_players:
            pending["media_player_entity_id"] = list(route.media_players)
            if player := self.hass.states.get(route.media_players[0]):
                pending["media_player_volume_level"] = player.attributes.get("volume_level")
        if (request := _request(route, pending)) is None:
            return announcement
        try:
            await self._call(*request)
        except Exception:
            _LOGGER.exception("Satellite Router couldn't play the announcement; the satellite will")
            return announcement
        if not conversation:
            return None
        return _cue(self.hass, announcement.preannounce_media_id, announcement.message)


def _request(route: Route, pending: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The action that plays a response, and its data."""
    if route.tts_script:
        return route.tts_script, pending
    if pending.get("message") and pending.get("media_player_entity_id"):
        # tts.speak rejects anything else (the patch also passed the
        # listening script's volume, which broke this fallback).
        return "tts.speak", {k: v for k, v in pending.items() if k in _TTS_SPEAK_FIELDS}
    return None


def _cue(hass: HomeAssistant, media_id: str | None, message: str) -> AssistSatelliteAnnouncement:
    """A short sound for the satellite to play before it starts listening.

    The device only opens its microphone for a conversation after playing an
    announcement, so one is needed even when the speech played elsewhere.
    """
    url = media_id or async_process_play_media_url(hass, PREANNOUNCE_URL)
    return AssistSatelliteAnnouncement(
        message=message,
        media_id=url,
        original_media_id=url,
        tts_token=None,
        media_id_source="url",
    )


def satellite_class() -> type:
    """ESPHome's satellite entity class."""
    from homeassistant.components.esphome.assist_satellite import (
        EsphomeAssistSatellite,
    )

    return EsphomeAssistSatellite


def _params(cls: type, name: str) -> list[str] | None:
    method = cls.__dict__.get(name)
    if method is None:
        return None
    return list(inspect.signature(getattr(method, _MARK, method)).parameters)


def check_compatible(cls: type) -> str | None:
    """Why the hook can't be installed on this ESPHome version, or None."""
    params = _params(cls, "on_pipeline_event")
    if params is None:
        return "EsphomeAssistSatellite.on_pipeline_event is missing"
    if params != ["self", "event"]:
        original = cls.__dict__["on_pipeline_event"]
        return f"on_pipeline_event{inspect.signature(getattr(original, _MARK, original))} has changed"
    return None


def install(cls: type, get_router: Callable[[], Router | None]) -> None:
    """Wrap ``cls.on_pipeline_event`` and the announcement methods (once)."""
    current = cls.__dict__["on_pipeline_event"]
    if not hasattr(current, _MARK):
        _install_events(cls, current, get_router)
    for name in _ANNOUNCE_METHODS:
        method = cls.__dict__.get(name)
        if method is None or hasattr(method, _MARK):
            continue
        # ESPHome's decorator hides the signature; core calls the base class's contract.
        params = _params(AssistSatelliteEntity, name)
        if params is None or len(params) != 2:
            _LOGGER.warning("ESPHome's %s has changed; announcements aren't routed", name)
            continue
        _install_announce(cls, name, method, get_router)


def _install_events(
    cls: type, original: Callable[..., Any], get_router: Callable[[], Router | None]
) -> None:
    def on_pipeline_event(self: Any, event: PipelineEvent) -> None:
        router = get_router()
        entry = getattr(self, "registry_entry", None)
        registry_id = entry.id if entry else None
        if router is not None:
            try:
                event = router.process(registry_id, event, self)
            except Exception:
                _LOGGER.exception("Satellite Router failed on %s", event.type)
        original(self, event)
        if router is not None:
            try:
                if router.take_silenced(registry_id, event):
                    # The device never played the response, so it will never report
                    # it finished; without this the entity stays "responding".
                    self.tts_response_finished()
            except Exception:
                _LOGGER.exception("Satellite Router failed to end the response on %s", event.type)

    setattr(on_pipeline_event, _MARK, original)
    on_pipeline_event.__doc__ = original.__doc__
    cls.on_pipeline_event = on_pipeline_event


def _install_announce(
    cls: type, name: str, original: Callable[..., Any], get_router: Callable[[], Router | None]
) -> None:
    conversation = name == "async_start_conversation"

    async def wrapper(self: Any, announcement: AssistSatelliteAnnouncement) -> None:
        router = get_router()
        if router is not None:
            entry = getattr(self, "registry_entry", None)
            try:
                result = await router.announce(
                    self, entry.id if entry else None, announcement, conversation
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Satellite Router failed on %s", name)
                result = announcement
            if result is None:
                return
            announcement = result
        await original(self, announcement)

    setattr(wrapper, _MARK, original)
    wrapper.__doc__ = original.__doc__
    setattr(cls, name, wrapper)


def uninstall(cls: type) -> None:
    """Restore ESPHome's own methods."""
    for name in ("on_pipeline_event", *_ANNOUNCE_METHODS):
        current = cls.__dict__.get(name)
        if current is not None and hasattr(current, _MARK):
            setattr(cls, name, getattr(current, _MARK))
