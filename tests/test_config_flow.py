"""Config and subentry flows, and the import from patched ESPHome options."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.satellite_router.const import DOMAIN


def _esphome(hass: HomeAssistant, title: str, options: dict) -> er.RegistryEntry:
    entry = MockConfigEntry(domain="esphome", title=title, options=options)
    entry.add_to_hass(hass)
    return er.async_get(hass).async_get_or_create(
        "assist_satellite",
        "esphome",
        f"{title}-sat",
        config_entry=entry,
        suggested_object_id=title.lower().replace(" ", "_") + "_assist_satellite",
    )


async def test_import_from_patched_options(hass: HomeAssistant) -> None:
    office = _esphome(
        hass,
        "Office Voice",
        {
            "allow_service_calls": True,
            "stt_script": "script.stt_listen",
            "tts_media_player_entity_id": ["media_player.office_sonos"],
            "tts_media_player_script": "script.tts_speak",
        },
    )
    _esphome(hass, "Garage Door", {"allow_service_calls": False})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["description_placeholders"] == {"count": "1", "titles": "Office Voice"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    [sub] = entry.subentries.values()
    assert sub.title == "Office Voice" and sub.unique_id == office.id
    assert dict(sub.data) == {
        "satellite": office.id,
        "media_players": ["media_player.office_sonos"],
        "stt_script": "script.stt_listen",
        "tts_script": "script.tts_speak",
    }

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["reason"] == "single_instance_allowed"


async def test_add_and_change_satellite(hass: HomeAssistant) -> None:
    sat = _esphome(hass, "Kitchen Voice", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Satellite Router")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "satellite"), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "satellite": sat.entity_id,
            "media_players": ["media_player.kitchen_sonos"],
            "tts_script": "script.tts_speak",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    [sub] = entry.subentries.values()
    assert dict(sub.data) == {
        "satellite": sat.id,
        "media_players": ["media_player.kitchen_sonos"],
        "tts_script": "script.tts_speak",
    }
    runtime = entry.runtime_data
    assert runtime.routes[sat.id].tts_script == "script.tts_speak"

    # The same satellite can't be added twice.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "satellite"), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"satellite": sat.entity_id}
    )
    assert result["reason"] == "already_configured"

    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"satellite": sat.entity_id, "media_players": []}
    )
    assert result["reason"] == "reconfigure_successful"
    assert runtime.routes[sat.id].media_players == ()
    assert runtime.routes[sat.id].tts_script is None
