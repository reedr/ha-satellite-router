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

Devices that run Assist pipelines themselves, such as a tablet using the
Companion app's Assist, aren't ESPHome satellites: their runs come over the
websocket and the app plays the response audio it is sent. For those, routes
are keyed by device and ``PipelineRun.process_event`` is wrapped. A routed
device's run is rewritten the same way, with two differences the Android app
needs: the run-start audio URL is removed (the app plays it at once), and the
tts-end URL is blanked rather than removed (the app can't parse a missing
one; a blank one it skips). A run that ends in a follow-up question is left
alone from intent-end on, so the device speaks the question itself; the app
reopens its microphone straight away when it has nothing to play, and would
hear the media players asking. The speakers stay ducked until the
conversation's last response. While such a run is going on,
``satellite_router.active_device`` names the device's area, for scripts that
need to know where the request came from.
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
from homeassistant.helpers import device_registry as dr

from .const import ACTIVE_DEVICE_STATE, DEVICE_KEY

_LOGGER = logging.getLogger(__name__)

_MARK = "__satellite_router_original__"
_TTS_SPEAK_FIELDS = ("entity_id", "message", "media_player_entity_id", "language", "options")
_ANNOUNCE_METHODS = ("async_announce", "async_start_conversation")
# A follow-up listen starts within seconds of its question having played; a
# kept volume older than this belongs to a conversation that never continued.
FOLLOW_UP_WINDOW = 120
# How long to wait for a media player to report the volume the listening script
# set (Sonos reports it a moment after the change), and for that script to finish
# before a response may restore the volume.
VOLUME_SETTLE = 2.0
LISTEN_WAIT = 10.0


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
        # Set once a run's listening script has finished and its volume change shows.
        self._listened: dict[str, asyncio.Event] = {}
        # Device routes whose current run ends in a follow-up question: left alone.
        self._passthrough: set[str] = set()

    @property
    def has_device_routes(self) -> bool:
        """Whether any route is for a device rather than an ESPHome satellite."""
        return any(key.startswith(DEVICE_KEY) for key in self.routes)

    @callback
    def process_device(self, device_id: str, event: PipelineEvent) -> PipelineEvent:
        """Act on an event of a device's own pipeline run; return the event it should get."""
        key = DEVICE_KEY + device_id
        route = self.routes.get(key)
        if route is None:
            return event
        data = event.data or {}
        if event.type is PipelineEventType.RUN_START:
            self._passthrough.discard(key)
            self._set_active(device_id)
        elif event.type is PipelineEventType.RUN_END:
            self._clear_active(device_id)
        if (
            event.type is PipelineEventType.INTENT_END
            and route.external_output
            and (data.get("intent_output") or {}).get("continue_conversation")
        ):
            # The device speaks the question and listens again; keep the speakers'
            # original volume for the conversation's last response.
            self._passthrough.add(key)
            pending = self._pending.pop(key, None) or {}
            if "media_player_volume_level" in pending:
                self._volume_before[key] = (
                    pending["media_player_volume_level"],
                    self.hass.loop.time(),
                )
            return event
        if key in self._passthrough:
            if event.type is PipelineEventType.RUN_END:
                self._passthrough.discard(key)
            return event
        original = data.get("tts_output")
        event = self.process(key, event)
        if event.type is PipelineEventType.TTS_END and original and not (event.data or {}).get("tts_output"):
            # No satellite to return to idle; and the app needs an (empty) URL.
            self._silenced.discard(key)
            return replace(event, data={**(event.data or {}), "tts_output": {**original, "url": ""}})
        return event

    def _set_active(self, device_id: str) -> None:
        device = dr.async_get(self.hass).async_get(device_id)
        area = device.area_id if device else None
        self.hass.states.async_set(
            ACTIVE_DEVICE_STATE,
            area or "unknown",
            {
                "device_id": device_id,
                "device": (device.name_by_user or device.name) if device else None,
                "area_id": area,
                "friendly_name": "Satellite Router active device",
            },
        )

    def _clear_active(self, device_id: str) -> None:
        current = self.hass.states.get(ACTIVE_DEVICE_STATE)
        if current is not None and current.attributes.get("device_id") == device_id:
            self.hass.states.async_set(
                ACTIVE_DEVICE_STATE, "idle", {"friendly_name": "Satellite Router active device"}
            )

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
                if self._call is not None and self._spawn is not None:
                    done = self._listened[registry_id] = asyncio.Event()
                    self._spawn(
                        self._listen(route, dict(pending), done), "satellite_router listen"
                    )
                else:
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
        listened = self._listened.get(registry_id)
        if (satellite := self._follow_up.pop(registry_id, None)) is not None:
            assert self._spawn is not None
            self._spawn(
                self._speak_then_listen(satellite, registry_id, route, *request, listened),
                "satellite_router follow-up",
            )
        elif listened is not None and not listened.is_set() and self._spawn is not None:
            # A run that ended at once (e.g. nothing heard): restoring the volume
            # before the listening script has ducked it would leave it ducked.
            self._spawn(self._run_after(listened, *request), "satellite_router response")
        else:
            self._run(*request)

    async def _listen(self, route: Route, data: dict[str, Any], done: asyncio.Event) -> None:
        """Run the listening script and wait until its volume change shows."""
        assert self._call is not None and route.stt_script is not None
        try:
            player = route.media_players[0] if route.media_players else None
            before = self._volume(player)
            await self._call(route.stt_script, data)
            if player:
                await self._volume_settled(player, before)
        except Exception:
            _LOGGER.exception("Satellite Router couldn't run the listening script")
        finally:
            done.set()

    async def _run_after(self, listened: asyncio.Event, action: str, data: dict[str, Any]) -> None:
        try:
            async with asyncio.timeout(LISTEN_WAIT):
                await listened.wait()
        except TimeoutError:
            pass
        self._run(action, data)

    def _volume(self, entity_id: str | None) -> Any:
        state = self.hass.states.get(entity_id) if entity_id else None
        return state.attributes.get("volume_level") if state else None

    async def _volume_settled(self, entity_id: str, before: Any) -> Any:
        """The player's volume once it differs from ``before`` (or after a short wait)."""
        deadline = self.hass.loop.time() + VOLUME_SETTLE
        while (volume := self._volume(entity_id)) == before and self.hass.loop.time() < deadline:
            await asyncio.sleep(0.1)
        return volume

    async def _speak_then_listen(
        self,
        satellite: Any,
        registry_id: str,
        route: Route,
        action: str,
        data: dict[str, Any],
        listened: asyncio.Event | None,
    ) -> None:
        """Play a response that asks something, then reopen the satellite's microphone."""
        assert self._call is not None
        if listened is not None:
            try:
                async with asyncio.timeout(LISTEN_WAIT):
                    await listened.wait()
            except TimeoutError:
                pass
        if "media_player_volume_level" in data and route.media_players:
            # The conversation isn't over: ask the script to leave the speakers as
            # they are (ducked), and restore the volume after the last response.
            self._volume_before[registry_id] = (
                data["media_player_volume_level"],
                self.hass.loop.time(),
            )
            current = self._volume(route.media_players[0])
            data = {**data, "media_player_volume_level": current}
        try:
            await self._call(action, data)
        except Exception:
            _LOGGER.exception("Satellite Router couldn't play the response; not listening again")
            self._restore_volume(registry_id, route)
            return
        self._keep_volume_fresh(registry_id)
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
        assert registry_id is not None
        if conversation and "media_player_volume_level" in pending:
            await self._duck(registry_id, route, pending)
        try:
            await self._call(*request)
        except Exception:
            _LOGGER.exception("Satellite Router couldn't play the announcement; the satellite will")
            self._restore_volume(registry_id, route)
            return announcement
        if not conversation:
            return None
        self._keep_volume_fresh(registry_id)
        return _cue(self.hass, announcement.preannounce_media_id, announcement.message)

    async def _duck(self, registry_id: str, route: Route, pending: dict[str, Any]) -> None:
        """Before a conversation's opening message: duck the speakers as for a listen.

        The original volume is kept for the conversation's last response, and the
        opening message is told to leave the speakers ducked.
        """
        if not route.stt_script or not route.media_players:
            return
        original = pending["media_player_volume_level"]
        try:
            await self._call(  # type: ignore[misc]
                route.stt_script,
                {
                    "media_player_entity_id": list(route.media_players),
                    "media_player_volume_level": original,
                },
            )
        except Exception:
            _LOGGER.exception("Satellite Router couldn't run the listening script")
            return
        self._volume_before[registry_id] = (original, self.hass.loop.time())
        # Sonos reports the new volume a moment later; reading it at once would
        # tell the response script to "restore" the unducked volume.
        pending["media_player_volume_level"] = await self._volume_settled(
            route.media_players[0], original
        )

    @callback
    def abandon(self, registry_id: str | None) -> None:
        """A conversation couldn't start on the satellite: give the volume back."""
        if registry_id and (route := self.routes.get(registry_id)):
            self._restore_volume(registry_id, route)

    def _keep_volume_fresh(self, registry_id: str) -> None:
        """Start the follow-up window once the message has played."""
        if registry_id in self._volume_before:
            self._volume_before[registry_id] = (
                self._volume_before[registry_id][0],
                self.hass.loop.time(),
            )


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
        try:
            await original(self, announcement)
        except Exception:
            if router is not None:
                entry = getattr(self, "registry_entry", None)
                router.abandon(entry.id if entry else None)
            raise

    setattr(wrapper, _MARK, original)
    wrapper.__doc__ = original.__doc__
    setattr(cls, name, wrapper)


def pipeline_run_class() -> type | None:
    """Core's pipeline run class, or None if it can't be found."""
    try:
        from homeassistant.components.assist_pipeline.run import PipelineRun
    except ImportError:
        try:
            from homeassistant.components.assist_pipeline.pipeline import PipelineRun
        except ImportError:
            return None
    return PipelineRun


def check_pipeline_compatible(cls: type | None) -> str | None:
    """Why device runs can't be routed on this core, or None."""
    if cls is None:
        return "assist_pipeline's PipelineRun is missing"
    params = _params(cls, "process_event")
    if params is None:
        return "PipelineRun.process_event is missing"
    if params != ["self", "event"]:
        return f"PipelineRun.process_event{params} has changed"
    fields = getattr(cls, "__dataclass_fields__", {})
    for name in ("device_id", "satellite_id"):
        # Public properties since 2026.11; before that only the private fields.
        if not isinstance(getattr(cls, name, None), property) and f"_{name}" not in fields:
            return f"PipelineRun.{name} is missing"
    return None


def _run_id(run: Any, name: str) -> str | None:
    """A run's device_id / satellite_id (property, or the older private field)."""
    value = getattr(type(run), name, None)
    if isinstance(value, property):
        return getattr(run, name)
    return getattr(run, f"_{name}", None)


def install_pipeline(cls: type, get_router: Callable[[], Router | None]) -> None:
    """Wrap ``PipelineRun.process_event`` (once), for routed devices' runs."""
    original = cls.__dict__["process_event"]
    if hasattr(original, _MARK):
        return

    def process_event(self: Any, event: PipelineEvent) -> None:
        router = get_router()
        # Satellite runs are routed by the satellite hook; only devices running
        # their own pipelines (no satellite) are handled here.
        if router is not None and router.has_device_routes and _run_id(self, "satellite_id") is None:
            device_id = _run_id(self, "device_id")
            if device_id:
                try:
                    event = router.process_device(device_id, event)
                except Exception:
                    _LOGGER.exception("Satellite Router failed on %s", event.type)
        original(self, event)

    setattr(process_event, _MARK, original)
    process_event.__doc__ = original.__doc__
    cls.process_event = process_event


def uninstall_pipeline(cls: type | None) -> None:
    """Restore core's own ``PipelineRun.process_event``."""
    current = cls.__dict__.get("process_event") if cls is not None else None
    if current is not None and hasattr(current, _MARK):
        cls.process_event = getattr(current, _MARK)


def uninstall(cls: type) -> None:
    """Restore ESPHome's own methods."""
    for name in ("on_pipeline_event", *_ANNOUNCE_METHODS):
        current = cls.__dict__.get(name)
        if current is not None and hasattr(current, _MARK):
            setattr(cls, name, getattr(current, _MARK))
