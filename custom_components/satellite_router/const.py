"""Constants for Satellite Router."""

DOMAIN = "satellite_router"

SUBENTRY_SATELLITE = "satellite"

CONF_SATELLITE = "satellite"  # entity registry ID of the assist_satellite entity
CONF_MEDIA_PLAYERS = "media_players"
CONF_STT_SCRIPT = "stt_script"
CONF_TTS_SCRIPT = "tts_script"

# The option keys the patched core ESPHome integration used on its entries.
LEGACY_TTS_MEDIA_PLAYER_ENTITY_ID = "tts_media_player_entity_id"
LEGACY_TTS_MEDIA_PLAYER_SCRIPT = "tts_media_player_script"
LEGACY_STT_SCRIPT = "stt_script"

ISSUE_INCOMPATIBLE = "incompatible_esphome"
ISSUE_PATCHED_CORE = "patched_core"
