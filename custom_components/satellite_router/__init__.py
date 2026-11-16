"""Satellite Router: send voice satellites' (and Assist devices') responses to other speakers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from homeassistant.components.esphome import const as esphome_const
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_ANNOUNCEMENTS,
    CONF_DEVICE,
    CONF_MEDIA_PLAYERS,
    CONF_SATELLITE,
    CONF_STT_SCRIPT,
    CONF_TTS_SCRIPT,
    DEVICE_KEY,
    DOMAIN,
    ISSUE_INCOMPATIBLE,
    ISSUE_PATCHED_CORE,
    PLAYBACK_TIMEOUT,
    SUBENTRY_DEVICE,
    SUBENTRY_SATELLITE,
)
from .router import (
    Route,
    Router,
    check_compatible,
    check_pipeline_compatible,
    install,
    install_pipeline,
    pipeline_run_class,
    satellite_class,
    uninstall,
    uninstall_pipeline,
)

_LOGGER = logging.getLogger(__name__)

type RouterConfigEntry = ConfigEntry[Router | None]

PLATFORMS = [Platform.SWITCH]  # voice mute switches of device routes


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

    async def call(action: str, data: dict[str, Any]) -> None:
        domain, service = action.split(".", 1)
        async with asyncio.timeout(PLAYBACK_TIMEOUT):
            await hass.services.async_call(domain, service, data, blocking=True)

    @callback
    def spawn(coro: Coroutine[Any, Any, None], name: str) -> None:
        entry.async_create_background_task(hass, coro, name)

    router = Router(hass, run, call, spawn)
    router.routes = routes_from(entry)
    entry.runtime_data = router
    install(cls, lambda: _active_router(hass))
    run_cls = pipeline_run_class()
    if (reason := check_pipeline_compatible(run_cls)) is not None:
        # Satellites still work; only devices running their own pipelines aren't routed.
        _LOGGER.warning("Can't route Assist devices' own pipeline runs: %s", reason)
    else:
        install_pipeline(run_cls, lambda: _active_router(hass))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def routes_from(entry: ConfigEntry) -> dict[str, Route]:
    """One route per satellite or device subentry."""
    routes: dict[str, Route] = {}
    for subentry in entry.subentries.values():
        data = subentry.data
        if subentry.subentry_type == SUBENTRY_SATELLITE:
            key = data[CONF_SATELLITE]
        elif subentry.subentry_type == SUBENTRY_DEVICE:
            key = DEVICE_KEY + data[CONF_DEVICE]
        else:
            continue
        routes[key] = Route(
            media_players=tuple(data.get(CONF_MEDIA_PLAYERS) or ()),
            stt_script=data.get(CONF_STT_SCRIPT) or None,
            tts_script=data.get(CONF_TTS_SCRIPT) or None,
            announcements=bool(data.get(CONF_ANNOUNCEMENTS)),
        )
    return routes


@callback
def _active_router(hass: HomeAssistant) -> Router | None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.runtime_data is not None:
            return entry.runtime_data
    return None


async def _async_update_listener(hass: HomeAssistant, entry: RouterConfigEntry) -> None:
    """Subentries changed: rebuild the routes in place (reload if devices came or went)."""
    if entry.runtime_data is not None:
        routes = routes_from(entry)
        devices = {key for key in routes if key.startswith(DEVICE_KEY)}
        if devices != {key for key in entry.runtime_data.routes if key.startswith(DEVICE_KEY)}:
            # Their voice mute switches have to be added or removed.
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
        entry.runtime_data.routes = routes


async def async_unload_entry(hass: HomeAssistant, entry: RouterConfigEntry) -> bool:
    """Restore ESPHome's own behaviour."""
    if entry.runtime_data is not None:
        if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
            return False
        uninstall(satellite_class())
        uninstall_pipeline(pipeline_run_class())
    return True
