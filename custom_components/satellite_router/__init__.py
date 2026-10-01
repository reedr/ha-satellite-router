"""Satellite Router: send ESPHome voice satellites' responses to other speakers."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.esphome import const as esphome_const
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_MEDIA_PLAYERS,
    CONF_SATELLITE,
    CONF_STT_SCRIPT,
    CONF_TTS_SCRIPT,
    DOMAIN,
    ISSUE_INCOMPATIBLE,
    ISSUE_PATCHED_CORE,
    SUBENTRY_SATELLITE,
)
from .router import Route, Router, check_compatible, install, satellite_class, uninstall

_LOGGER = logging.getLogger(__name__)

type RouterConfigEntry = ConfigEntry[Router | None]


async def async_setup_entry(hass: HomeAssistant, entry: RouterConfigEntry) -> bool:
    """Hook ESPHome's satellites, unless that would conflict or can't work."""
    entry.runtime_data = None
    for issue in (ISSUE_INCOMPATIBLE, ISSUE_PATCHED_CORE):
        ir.async_delete_issue(hass, DOMAIN, issue)

    # A core patched with the same feature would route every response twice.
    if hasattr(esphome_const, "CONF_TTS_MEDIA_PLAYER_SCRIPT"):
        _LOGGER.error("ESPHome in this Home Assistant is patched to route satellites itself")
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_PATCHED_CORE,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_PATCHED_CORE,
        )
        return True

    cls = satellite_class()
    if (reason := check_compatible(cls)) is not None:
        _LOGGER.error("Can't route ESPHome satellites: %s", reason)
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_INCOMPATIBLE,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_INCOMPATIBLE,
            translation_placeholders={"reason": reason},
        )
        return True

    @callback
    def run(action: str, data: dict[str, Any]) -> None:
        domain, service = action.split(".", 1)
        entry.async_create_background_task(
            hass,
            hass.services.async_call(domain, service, data),
            f"{DOMAIN} {action}",
        )

    router = Router(hass, run)
    router.routes = routes_from(entry)
    entry.runtime_data = router
    install(cls, lambda: _active_router(hass))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


@callback
def routes_from(entry: ConfigEntry) -> dict[str, Route]:
    """One route per satellite subentry."""
    routes: dict[str, Route] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_SATELLITE:
            continue
        data = subentry.data
        routes[data[CONF_SATELLITE]] = Route(
            media_players=tuple(data.get(CONF_MEDIA_PLAYERS) or ()),
            stt_script=data.get(CONF_STT_SCRIPT) or None,
            tts_script=data.get(CONF_TTS_SCRIPT) or None,
        )
    return routes


@callback
def _active_router(hass: HomeAssistant) -> Router | None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.runtime_data is not None:
            return entry.runtime_data
    return None


async def _async_update_listener(hass: HomeAssistant, entry: RouterConfigEntry) -> None:
    """Subentries changed: rebuild the routes in place (no reload needed)."""
    if entry.runtime_data is not None:
        entry.runtime_data.routes = routes_from(entry)


async def async_unload_entry(hass: HomeAssistant, entry: RouterConfigEntry) -> bool:
    """Restore ESPHome's own behaviour."""
    if entry.runtime_data is not None:
        uninstall(satellite_class())
    return True
