# Satellite Router

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/reedr/ha-satellite-router)

Plays ESPHome voice satellites' responses on other media players (Sonos, for example) instead of the satellite's
speaker, and runs a script when a satellite starts listening (e.g. to duck the room's music). Tablets using the
Companion app's Assist can be routed the same way. No changes to Home
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

### Conversations and announcements

A satellite only opens its microphone for a conversation after it has played something, and a routed satellite
plays nothing. So for routed satellites:

- **`assist_satellite.start_conversation` / `ask_question`**: the listening script runs first (so the speakers are
  ducked as for any conversation), the opening message plays on the media players (through the response script,
  waiting until it has finished), then the satellite plays the pre-announcement chime and listens, without a wake
  word. The speakers stay ducked until the conversation's last response.
- **Follow-up questions** (a response with `continue_conversation`, e.g. an LLM asking "which room?"): the response
  plays on the media players, then the satellite gets the same chime and listens again. The speakers stay ducked
  in between: the response script is passed the current (ducked) volume as `media_player_volume_level`, and the
  original volume is passed again with the conversation's last response.
- **`assist_satellite.announce`**: plays on the satellite unless the route's **Route announcements** option is on.
  Sounds and media URLs always play on the satellite.

If the media players can't play a message, the satellite plays it itself.

`media_player_volume_level` is the volume the response script should leave the media players at once it has
spoken; the listening script gets the volume they were at.

If an ESPHome update changes the code this hooks into, Satellite Router stays inactive and raises a repair, and
satellites go back to playing responses themselves until it is updated. It also stays inactive, with a repair, on a
Home Assistant whose ESPHome integration has been patched with the same feature, so responses never play twice.

### Devices that run Assist themselves (Companion app tablets)

A tablet using the Home Assistant Companion app's Assist isn't an ESPHome satellite: it runs the pipeline itself
over the websocket and plays the response audio it is sent. Such a device can be routed too, keyed by its device
(the Android app sends its device ID with every request). Satellite Router then wraps core's pipeline run for that
device's requests, and only those:

- listening runs the **listening script**, the response plays through the **response script** (or `tts.speak`),
  and the device shows the text but plays nothing. The audio URL in the run-start event is removed (the app plays it
  at once) and the one in tts-end is blanked rather than removed (the app can't parse a missing one).
- **Follow-up questions** are spoken by the device itself: with nothing to play, the app reopens its microphone at
  once and would hear the media players asking. The speakers stay ducked until the conversation's last response,
  which restores their original volume.
- While such a request is going on, the state `satellite_router.active_device` holds the device's area (state) and
  device ID / name (attributes), and is `idle` otherwise. Scripts can use it to know which room a request came from,
  for example to play music "here".
- Each device gets a **Voice mute** switch on its own device page (e.g. `switch.office_tablet_voice_mute`). Turning
  it on sends the Android app the `command_wake_word_detection` notification command to turn its wake word off
  (this needs Home Assistant to be the device's default digital assistant app), and while it is on any spoken
  request that still arrives is ended before speech-to-text, so its audio isn't transcribed and nothing runs.
  Typed requests still work. Turning it off turns the wake word back on. The switch keeps its state across restarts.

If a Home Assistant update changes the pipeline code this hooks into, a warning is logged and devices go back to
playing responses themselves; satellites are unaffected.

## Setup

**Settings → Devices & services → Add integration → Satellite Router**. If the ESPHome entries carry the options of
the old core patch (`tts_media_player_entity_id`, `tts_media_player_script`, `stt_script`), those routes are imported.

Add or change satellites with **Add satellite** on the integration's page:

- **Satellite**: an ESPHome `assist_satellite` entity.
- **Media players**: where responses play.
- **Listening script** (optional): runs when the satellite starts listening.
- **Response script** (optional): plays the response; without it, `tts.speak` is used.

Add a device with **Add device**: pick the Companion app device (Mobile App integration), and its media players and
scripts as above.

Changes take effect immediately.

## License

MIT. See [LICENSE](LICENSE).
