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
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

_MARK = "__satellite_router_original__"


@dataclass(frozen=True, slots=True)
class Route:
    """Where one satellite's speech goes."""

    media_players: tuple[str, ...]
    stt_script: str | None
    tts_script: str | None

    @property
    def external_output(self) -> bool:
        """Whether responses play somewhere other than the satellite."""
        return bool(self.tts_script or self.media_players)


class Router:
    """Holds the routes and each satellite's response in progress."""

    def __init__(self, hass: HomeAssistant, run: Callable[[str, dict[str, Any]], None]) -> None:
        """Set up; ``run(action, data)`` calls an action without waiting for it."""
        self.hass = hass
        self._run = run
        self.routes: dict[str, Route] = {}  # by entity registry ID
        self._pending: dict[str, dict[str, Any]] = {}

    @callback
    def process(self, registry_id: str | None, event: PipelineEvent) -> PipelineEvent:
        """Act on an event for a satellite; return the event ESPHome should see."""
        route = self.routes.get(registry_id) if registry_id else None
        if route is None:
            return event
        data = event.data or {}

        if event.type is PipelineEventType.STT_START:
            if route.stt_script:
                pending: dict[str, Any] = {}
                if route.media_players and (player := self.hass.states.get(route.media_players[0])):
                    pending["media_player_volume_level"] = player.attributes.get("volume_level")
                    pending["media_player_entity_id"] = list(route.media_players)
                self._pending[registry_id] = pending
                self._run(route.stt_script, dict(pending))

        elif event.type is PipelineEventType.INTENT_PROGRESS:
            if route.external_output and data.get("tts_start_streaming"):
                # The satellite won't play the response, so it needn't prepare to stream it.
                return replace(event, data={**data, "tts_start_streaming": False})

        elif event.type is PipelineEventType.TTS_START:
            if route.media_players:
                pending = self._pending.setdefault(registry_id, {})
                pending.update(
                    {
                        "entity_id": data.get("engine"),
                        "message": data.get("tts_input"),
                        "media_player_entity_id": list(route.media_players),
                    }
                )
                if data.get("language"):
                    pending["language"] = data["language"]
                if data.get("voice"):
                    pending["options"] = {"voice": data["voice"]}

        elif event.type is PipelineEventType.TTS_END:
            self._speak(registry_id, route)
            if route.external_output and data.get("tts_output"):
                return replace(event, data={**data, "tts_output": None})

        elif event.type is PipelineEventType.RUN_START:
            if route.external_output and data.get("tts_output"):
                return replace(event, data={k: v for k, v in data.items() if k != "tts_output"})

        elif event.type is PipelineEventType.RUN_END:
            self._speak(registry_id, route)

        return event

    def _speak(self, registry_id: str, route: Route) -> None:
        """Hand a collected response to the TTS script, or to tts.speak."""
        pending = self._pending.get(registry_id)
        if not pending:
            return
        action = route.tts_script or ("tts.speak" if pending.get("message") else None)
        if action is None:
            return
        del self._pending[registry_id]
        self._run(action, pending)


def satellite_class() -> type:
    """ESPHome's satellite entity class."""
    from homeassistant.components.esphome.assist_satellite import (
        EsphomeAssistSatellite,
    )

    return EsphomeAssistSatellite


def check_compatible(cls: type) -> str | None:
    """Why the hook can't be installed on this ESPHome version, or None."""
    method = cls.__dict__.get("on_pipeline_event")
    if method is None:
        return "EsphomeAssistSatellite.on_pipeline_event is missing"
    original = getattr(method, _MARK, method)
    if list(inspect.signature(original).parameters) != ["self", "event"]:
        return f"on_pipeline_event{inspect.signature(original)} has changed"
    return None


def install(cls: type, get_router: Callable[[], Router | None]) -> None:
    """Wrap ``cls.on_pipeline_event`` (once)."""
    current = cls.__dict__["on_pipeline_event"]
    if hasattr(current, _MARK):
        return
    original = current

    def on_pipeline_event(self: Any, event: PipelineEvent) -> None:
        router = get_router()
        if router is not None:
            entry = getattr(self, "registry_entry", None)
            try:
                event = router.process(entry.id if entry else None, event)
            except Exception:
                _LOGGER.exception("Satellite Router failed on %s", event.type)
        original(self, event)

    setattr(on_pipeline_event, _MARK, original)
    on_pipeline_event.__doc__ = original.__doc__
    cls.on_pipeline_event = on_pipeline_event


def uninstall(cls: type) -> None:
    """Restore ESPHome's own method."""
    current = cls.__dict__.get("on_pipeline_event")
    if current is not None and hasattr(current, _MARK):
        cls.on_pipeline_event = getattr(current, _MARK)
