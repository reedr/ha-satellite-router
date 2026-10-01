"""Config flow for Satellite Router."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_MEDIA_PLAYERS,
    CONF_SATELLITE,
    CONF_STT_SCRIPT,
    CONF_TTS_SCRIPT,
    DOMAIN,
    LEGACY_STT_SCRIPT,
    LEGACY_TTS_MEDIA_PLAYER_ENTITY_ID,
    LEGACY_TTS_MEDIA_PLAYER_SCRIPT,
    SUBENTRY_SATELLITE,
)

ESPHOME = "esphome"


def legacy_routes(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Subentries for satellites configured by the patched ESPHome's options."""
    ent_reg = er.async_get(hass)
    found = []
    for esphome_entry in hass.config_entries.async_entries(ESPHOME):
        opts = esphome_entry.options
        players = opts.get(LEGACY_TTS_MEDIA_PLAYER_ENTITY_ID) or []
        if isinstance(players, str):
            players = [players]
        stt, tts = (
            opts.get(LEGACY_STT_SCRIPT) or None,
            opts.get(LEGACY_TTS_MEDIA_PLAYER_SCRIPT) or None,
        )
        if not (players or stt or tts):
            continue
        satellites = [
            e
            for e in er.async_entries_for_config_entry(ent_reg, esphome_entry.entry_id)
            if e.domain == "assist_satellite"
        ]
        if not satellites:
            continue
        sat = satellites[0]
        data = {CONF_SATELLITE: sat.id, CONF_MEDIA_PLAYERS: list(players)}
        if stt:
            data[CONF_STT_SCRIPT] = stt
        if tts:
            data[CONF_TTS_SCRIPT] = tts
        found.append(
            {
                "data": data,
                "subentry_type": SUBENTRY_SATELLITE,
                "title": esphome_entry.title,
                "unique_id": sat.id,
            }
        )
    return found


class RouterConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry; satellites are its subentries."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Satellites are added as subentries."""
        return {SUBENTRY_SATELLITE: SatelliteSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the entry, importing routes from patched ESPHome options if any."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        found = legacy_routes(self.hass)
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                description_placeholders={
                    "count": str(len(found)),
                    "titles": ", ".join(r["title"] for r in found) or "none",
                },
            )
        return self.async_create_entry(title="Satellite Router", data={}, subentries=found)


def _schema(hass: HomeAssistant) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SATELLITE): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="assist_satellite", integration=ESPHOME)
            ),
            vol.Optional(CONF_MEDIA_PLAYERS): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="media_player", multiple=True)
            ),
            vol.Optional(CONF_STT_SCRIPT): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="script")
            ),
            vol.Optional(CONF_TTS_SCRIPT): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="script")
            ),
        }
    )


class SatelliteSubentryFlow(ConfigSubentryFlow):
    """Add or change one satellite's route."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Choose a satellite and where its speech goes."""
        return await self._async_step("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change a route."""
        return await self._async_step("reconfigure", user_input)

    async def _async_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        ent_reg = er.async_get(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            sat = ent_reg.async_get(user_input[CONF_SATELLITE])
            if sat is None:
                errors[CONF_SATELLITE] = "unknown_satellite"
            else:
                data = {
                    CONF_SATELLITE: sat.id,
                    CONF_MEDIA_PLAYERS: user_input.get(CONF_MEDIA_PLAYERS) or [],
                }
                for key in (CONF_STT_SCRIPT, CONF_TTS_SCRIPT):
                    if user_input.get(key):
                        data[key] = user_input[key]
                title = sat.name or sat.original_name or sat.entity_id
                if step_id == "reconfigure":
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._get_reconfigure_subentry(),
                        data=data,
                        title=title,
                        unique_id=sat.id,
                    )
                for sub in self._get_entry().subentries.values():
                    if sub.unique_id == sat.id:
                        return self.async_abort(reason="already_configured")
                return self.async_create_entry(title=title, data=data, unique_id=sat.id)

        suggested: dict[str, Any] = dict(user_input or {})
        if step_id == "reconfigure" and user_input is None:
            current = dict(self._get_reconfigure_subentry().data)
            if (sat := ent_reg.async_get(current[CONF_SATELLITE])) is not None:
                current[CONF_SATELLITE] = sat.entity_id
            suggested = current
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(_schema(self.hass), suggested),
            errors=errors,
        )
