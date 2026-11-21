# Changelog

## V4.3

- Status page: the latest **5** subtitles (was 20). The page **Subtitles** still shows up to 500.

## V4.2

- Status page: the latest **20** subtitles.
- New page **Subtitles** next to Status: up to **500** subtitles, newest at the top, with the channel name, and buttons for bigger/smaller text (remembered per device). Made for a tablet next to the TV.
- The subtitle history is kept when you zap to another channel.
- Light on the network: the page only fetches the new lines (`/api/subtitles?after=`).

## V4.1

- Status page: the latest **100** subtitles (was 30), handy on a tablet next to the TV.
- Waiting screen in Kodi: only "Translating - please wait" (no more "±40 seconds").
- Private add-on included: Whisper speech server for an NVIDIA GPU (`docker-compose.whisper.yml`, `services/whisper/`), with the same versions as the old built-in version. Not published to git (listed in `.gitignore`).

## V4 – choose everything on the web page, no stalls from a slow server

- New page **Translation** in the web interface: choose the language of the channels, the subtitle language, the allowed languages, the speech recognition (address, model, key) and the translator (LibreTranslate/NLLB or LLM). With **test buttons**; changes work right away without a restart.
- The status page shows a **red warning** when the speech recognition or translator does not work, with the reason.
- The program starts even when the speech recognition is not set up yet (channels play without subtitles); set it up on the web page.
- **No more stalling picture from a slow or unreachable speech server**: waiting is limited to 60% of a piece (`ASR_TIMEOUT=auto`), the translator to 35% (`TRANSLATE_TIMEOUT=auto`), and after an error the pause grows (10, 20, 40, 60 s). Before, every piece could wait 30 s, which emptied Kodi's buffer.
- No second recognition attempt when the first one was already slow.
- Container based on Ubuntu 24.04 again, with the ffmpeg 6.1 the stream joining was tested with.

## V3 – new name: M3U Translator Subtitle

- New name: **M3U Translator Subtitle** (was M3U Translator). Container: `m3u-translator-subtitle`.
- A ready-to-use `.env` is included in the release zip (same values as `.env.example`); only `ASR_URL` and `TRANSLATE_URL` need filling in.

## V2 – built-in web interface

- Web interface at `http://IP-OF-THIS-PC:8088/` with three pages: **Status**, **Channels** and **Subtitle style**.
- Status page: active channel, state, language, buffer, delay, and the **latest subtitles** (translation with the original underneath), refreshed every 3 seconds.
- Channels page: all channels per group with a search box; the channel being translated is marked.
- The Kodi address with a copy button.
- `/` serves the web interface to a browser and the channel list to Kodi, so existing Kodi setups keep working. `/settings` opens the subtitle style page directly.
- New JSON endpoints: `/api/status` (with the latest subtitles) and `/api/channels`.

## V1 – first public version

**M3U Translator**: live translated subtitles for IPTV channels from an M3U list, for playback in Kodi.

- Speech recognition of your choice (`ASR_ENGINE`): `http` (any OpenAI-compatible speech API), `command` (own program) or `plugin` (own Python file). No speech model built in; the container needs no GPU.
- Translator of your choice (`TRANSLATE_ENGINE`): `libretranslate` (also the optional NLLB service) or `llm` (language model via an OpenAI-compatible chat API).
- Voice activity detection (Silero VAD) on the CPU: no speech recognition during silence or music.
- Automatic language detection per channel with allowlist and confirmed language changes.
- DVB subtitles inside the stream, video and audio untouched; the language label follows `TARGET_LANG`.
- Subtitles following the guidelines for deaf and hard-of-hearing viewers: max. 42 characters per line, logical line breaks, reading-speed planner.
- Web page `/settings` to change font, size, outline, colours, position and reading pace live, with a preview and optional password.
- Waiting screen in exactly the channel's format, seamless timeline to Kodi, robust against broken pieces and source time jumps.
- English documentation and code, MIT licence.

## Before V1 (private versions v0.1 – v0.43)

The code comments refer to these versions as v0.xx.

- **v0.41**: no speech recognition during silence or music; the subtitle no longer blinks on the border of two pieces.
- **v0.40**: waiting screen in exactly the channel's format (no wrong codec or picture size for Kodi when zapping); a piece with broken audio continues with silence; nonsense times in the source handled.
- **v0.39**: language filter (`LANGUAGE_ALLOWLIST`, `LANGUAGE_ACCEPT_PROBABILITY`), language change only after two checks.
- **v0.38**: second speech recognition on the CPU as a trial, with a safety net.
- **v0.37**: at most 42 characters per line, line breaks at logical places.
- **v0.36**: reading-speed planner (a line only leaves when it could have been read).
- **v0.35**: open unknown resolutions again; one broken piece no longer stops the channel.
- **v0.34**: strict maximum resolution; restart the pipeline after a fatal error.
- **v0.33**: carry an unfinished sentence over to the next piece (no repetition or loose tail).
- **v0.32**: long sentences in parts after each other; a duplicate short sentence shown once.
- **v0.31**: own continuous timeline to Kodi; subtitles never in the wrong order; per-minute overview.
- **v0.30**: pinned versions, update check script, system check at start.
- **v0.29**: one continuous TS stream to Kodi; the subtitle gets a language label.
- **v0.28**: thick round outline with soft edges, no box.
- **v0.26**: older sentence beige, newest white.
- **v0.25**: waiting screen in the channel's format.
- **v0.24**: translate half sentences together; checks for made-up and dropped text.
- **v0.20–v0.23**: remove repetition caused by the overlap; NLLB translator; glossary.
- **v0.17**: subtitle as DVB subtitle inside the stream.
- **v0.14–v0.16**: separate subtitle next to the original picture.
- **v0.11–v0.13**: automatic language detection per channel; less missed speech.
- **v0.5–v0.10**: all channels in Kodi, start automatically when zapping, waiting screen, groups.
- **v0.1–v0.4**: first working version, filters against repetition and made-up text.
