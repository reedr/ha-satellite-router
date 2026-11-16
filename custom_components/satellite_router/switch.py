"""Voice mute switches for devices that run Assist themselves (e.g. a Companion app tablet).

On: the Android app is asked to turn its wake word off, and any spoken request
from the device that still arrives is ended before speech-to-text (see router).
Off: the app is asked to turn its wake word back on. The state survives restarts.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import slugify

from . import RouterConfigEntry
from .const import CONF_DEVICE, SUBENTRY_DEVICE

_LOGGER = logging.getLogger(__name__)

MOBILE_APP = "mobile_app"
WAKE_WORD_COMMAND = "command_wake_word_detection"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RouterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One voice mute switch per device route."""
    dev_reg = dr.async_get(hass)
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_DEVICE:
            continue
        device = dev_reg.async_get(subentry.data[CONF_DEVICE])
        if device is None:
            continue
        async_add_entities(
            [VoiceMuteSwitch(entry, device)],
            config_subentry_id=subentry.subentry_id,
        )


def notify_service(hass: HomeAssistant, device: dr.DeviceEntry) -> str | None:
    """The device's legacy ``notify.mobile_app_*`` action (it takes command data), if any."""
    for entry_id in device.config_entries:
        app = hass.config_entries.async_get_entry(entry_id)
        if app is None or app.domain != MOBILE_APP:
            continue
        name = app.data.get("device_name") or app.title
        service = f"mobile_app_{slugify(name)}"
        if hass.services.has_service("notify", service):
            return service
    return None


class VoiceMuteSwitch(SwitchEntity, RestoreEntity):
    """Mutes a device's voice assistant."""

    _attr_has_entity_name = True
    _attr_translation_key = "voice_mute"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: RouterConfigEntry, device: dr.DeviceEntry) -> None:
        """Attach to the device's own registry entry (e.g. the Companion app's)."""
        self._entry = entry
        self._device_id = device.id
        self._attr_unique_id = f"device:{device.id}_voice_mute"
        # Joins the device as template helpers do (device_info would make a second device).
        self.device_entry = device
        # switch.<device>_voice_mute, like the ESPHome satellites' (no area prefix).
        self.entity_id = f"switch.{slugify(device.name_by_user or device.name or device.id)}_voice_mute"
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        """Restore the last state (without re-sending the app command)."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._set(last.state == STATE_ON)

    async def async_will_remove_from_hass(self) -> None:
        """Stop dropping the device's requests."""
        if (router := self._entry.runtime_data) is not None:
            router.muted.discard(self._device_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Mute."""
        self._set(True)
        self.async_write_ha_state()
        await self._wake_word("turn_off")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unmute."""
        self._set(False)
        self.async_write_ha_state()
        await self._wake_word("turn_on")

    def _set(self, on: bool) -> None:
        self._attr_is_on = on
        if (router := self._entry.runtime_data) is not None:
            if on:
                router.muted.add(self._device_id)
            else:
                router.muted.discard(self._device_id)

    async def _wake_word(self, command: str) -> None:
        """Ask the Android app to turn its wake word on or off (best effort)."""
        device = dr.async_get(self.hass).async_get(self._device_id)
        service = notify_service(self.hass, device) if device else None
        if service is None:
            _LOGGER.warning(
                "No notify.mobile_app action for %s; its requests are dropped but the wake word stays on",
                self.entity_id,
            )
            return
        try:
            await self.hass.services.async_call(
                "notify",
                service,
                {"message": WAKE_WORD_COMMAND, "data": {"command": command}},
                blocking=True,
            )
        except Exception:
            _LOGGER.warning("Couldn't send %s %s to %s", WAKE_WORD_COMMAND, command, service, exc_info=True)
