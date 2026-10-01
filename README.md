# Satellite Router

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/reedr/ha-satellite-router)

Plays ESPHome voice satellites' responses on other media players (Sonos, for example) instead of the satellite's
speaker, and runs a script when a satellite starts listening (e.g. to duck the room's music). No changes to Home
Assistant or ESPHome are needed.

## How it works

ESPHome's satellite entity turns each Assist pipeline event into a message for the device. Satellite Router wraps
that step for the satellites you route:

| Pipeline event | What happens |
|---|---|
| Listening starts (STT start) | The **listening script** runs with `media_player_entity_id` (the route's media players) and `media_player_volume_level` (the first player's current volume). |
| Response text (TTS start) | The text, TTS engine, language and voice are kept for the response. |
| Response audio (TTS end) | The **response script** runs with `entity_id` (TTS engine), `message`, `media_player_entity_id`, `language`, `options` (`voice`) and the volume above. Without a response script, `tts.speak` plays the message on the media players. The satellite gets the event without the audio, so it stays silent. |
| Early streaming, announcement audio | Removed for routed satellites, so the satellite doesn't prepare to play them. |

Everything else ESPHome does is untouched, and satellites without a route behave normally.

If an ESPHome update changes the code this hooks into, Satellite Router stays inactive and raises a repair, and
satellites go back to playing responses themselves until it is updated. It also stays inactive, with a repair, on a
Home Assistant whose ESPHome integration has been patched with the same feature, so responses never play twice.

## Setup

**Settings → Devices & services → Add integration → Satellite Router**. If the ESPHome entries carry the options of
the old core patch (`tts_media_player_entity_id`, `tts_media_player_script`, `stt_script`), those routes are imported.

Add or change satellites with **Add satellite** on the integration's page:

- **Satellite**: an ESPHome `assist_satellite` entity.
- **Media players**: where responses play.
- **Listening script** (optional): runs when the satellite starts listening.
- **Response script** (optional): plays the response; without it, `tts.speak` is used.

Changes take effect immediately.

## License

MIT. See [LICENSE](LICENSE).
