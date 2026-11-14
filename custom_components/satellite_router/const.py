"""Constants for Satellite Router."""

DOMAIN = "satellite_router"

SUBENTRY_SATELLITE = "satellite"
SUBENTRY_DEVICE = "device"  # an Assist device that isn't an ESPHome satellite (e.g. a Companion app tablet)

CONF_SATELLITE = "satellite"  # entity registry ID of the assist_satellite entity
CONF_DEVICE = "device"  # device registry ID of a device that runs Assist pipelines itself
CONF_MEDIA_PLAYERS = "media_players"
CONF_STT_SCRIPT = "stt_script"
CONF_TTS_SCRIPT = "tts_script"
CONF_ANNOUNCEMENTS = "announcements"  # also route assist_satellite.announce

# Routes of devices are keyed by this prefix + device registry ID (satellites by entity registry ID).
DEVICE_KEY = "device:"
# While a routed device's run is going on, this state names the device and its area.
ACTIVE_DEVICE_STATE = "satellite_router.active_device"

# How long a response or announcement may take to play on the media players.
PLAYBACK_TIMEOUT = 300

# The option keys the patched core ESPHome integration used on its entries.
LEGACY_TTS_MEDIA_PLAYER_ENTITY_ID = "tts_media_player_entity_id"
LEGACY_TTS_MEDIA_PLAYER_SCRIPT = "tts_media_player_script"
LEGACY_STT_SCRIPT = "stt_script"

ISSUE_INCOMPATIBLE = "incompatible_esphome"
ISSUE_PATCHED_CORE = "patched_core"
