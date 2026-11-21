# M3U Translator Subtitle

Live translated subtitles for IPTV channels in **Kodi**.

```
IPTV channel ─▶ voice detection ─▶ speech recognition ─▶ translator ─▶ subtitle in the stream ─▶ Kodi
                (CPU)              (your choice)          (your choice)  (DVB, on/off in Kodi)
```

*Works with any channel in an M3U list and any language pair. This is an independent project; it is not affiliated with or endorsed by Kodi / the XBMC Foundation or any broadcaster.*

- Video and audio stay original: nothing is re-encoded.
- The subtitle is a **DVB subtitle** inside the stream, like on digital TV. Kodi switches it on or off with the subtitle button and shows it with the right language label (for example *Dutch*).
- Zap to a channel in Kodi and the translation starts by itself; when nobody is watching, it stops.
- **No speech model built in.** You connect your own speech recognition and translator: a server on your network, an online service, a language model (LLM) or your own script.
- **Built-in web interface** at `http://IP-OF-THIS-PC:8088/`: status, latest subtitles, channel list, and the subtitle style (font, size, outline, colours, position, reading pace) live with a preview.
- Subtitles follow the guidelines for deaf and hard-of-hearing viewers: at most 42 characters per line, line breaks at logical places, long enough on screen to read.

One channel at a time. You watch about half a minute behind live.

---

## Contents

1. [What you need](#what-you-need)
2. [Quick start](#quick-start)
3. [Connecting speech recognition](#connecting-speech-recognition)
4. [Connecting a translator](#connecting-a-translator)
5. [Web interface](#web-interface)
6. [Setting up Kodi](#setting-up-kodi)
7. [Channels](#channels)
8. [All settings](#all-settings)
9. [Checking and troubleshooting](#checking-and-troubleshooting)
10. [Folders](#folders)
11. [Support and licence](#support-and-licence)

---

## What you need

- A Linux PC or server with **Docker** and **Docker Compose**.
- **A speech recognition** (speech-to-text). See [below](#connecting-speech-recognition).
- **A translator**: a LibreTranslate server, the included NLLB service or a language model.
- **Kodi** with the add-on *PVR IPTV Simple Client*.

The translator container itself needs no GPU.

## Quick start

```bash
# Clone the repository
git clone https://github.com/Kristofdr86/m3u-translator-subtitle.git
cd m3u-translator-subtitle

# Create your configuration
cp .env.example .env
nano .env

# Download channel lists (optional)
./scripts/fetch-channels.sh UK uk

# Or place your own M3U playlists in the channels/ directory

# Build and start
docker compose up -d --build

# View logs
docker compose logs -f translator
```

At start the log shows which speech recognition and translator are used, and then:

```
Ready. In Kodi use: http://IP-OF-THIS-PC:8088/kodi.m3u
Web interface: http://IP-OF-THIS-PC:8088/
```

**Test first without a speech model?** Put this in `.env`:

```
ASR_ENGINE=plugin
ASR_PLUGIN=/config/examples/asr-plugin-example.py
```

Then `[speech 2.4 s]` appears on screen where someone speaks. This way you know that channel, stream and Kodi work.

---

## Connecting speech recognition

Choose one of three ways with `ASR_ENGINE`. The program does voice activity detection itself (Silero VAD on the CPU): during silence or music the speech recognition is not called at all.

### `http` – a server with an OpenAI-compatible API

Any server or service with the endpoint `POST /v1/audio/transcriptions` works, for example your own server such as Speaches or LocalAI, or an online service.

```
ASR_ENGINE=http
ASR_URL=http://192.168.1.20:8000/v1
ASR_MODEL=                     # model name on the server (empty = server default)
ASR_API_KEY=                   # only when the server asks for a key
ASR_RESPONSE_FORMAT=verbose_json
```

- `verbose_json` gives sentences with start and end times: subtitles then appear exactly on time. If the server can only do `json` or `text`, the sentences are spread over the speech automatically.
- If the server returns the **language**, automatic language detection per channel works (`SOURCE_LANG=auto`). If not, set `lang="xx"` per channel in the channel list together with `USE_PLAYLIST_LANG=1`, or one fixed `SOURCE_LANG`.
- If the server can also translate to English (`/v1/audio/translations`), languages your translator does not know go to English first. If it cannot, this is detected automatically (or set `ASR_TRANSLATE=0`).

### `command` – your own program

```
ASR_ENGINE=command
ASR_COMMAND=python3 /config/my-asr.py
```

For every piece the program is started with the path to a WAV file (16 kHz, mono). The environment contains `ASR_LANGUAGE`, `ASR_TASK` and `ASR_PROMPT`. The program writes JSON or plain text to stdout. See `examples/asr-command-example.py`.

### `plugin` – your own Python file

```
ASR_ENGINE=plugin
ASR_PLUGIN=/config/my_asr.py
```

The file has a function `transcribe(audio, sample_rate=16000, language=None, task="transcribe", prompt=None)` and optionally `setup()`. It stays loaded, so a model can stay in memory. See `examples/asr-plugin-example.py`. Install extra Python packages in your own image (`FROM` this image + `pip install`).

### What a connection returns

```json
{"language": "en", "language_probability": 0.97,
 "segments": [{"start": 0.0, "end": 2.4, "text": "Good evening."}]}
```

Only `segments` (or `text`) is required. If the server also returns `avg_logprob`, `no_speech_prob` or `compression_ratio` per sentence, made-up sentences (during music or silence) are filtered out.

---

## Connecting a translator

### `libretranslate` (default)

```
TRANSLATE_ENGINE=libretranslate
TRANSLATE_URL=http://192.168.1.20:5000
```

Works with a LibreTranslate server and with the included **NLLB service** (Meta NLLB-200 on an NVIDIA GPU, often better with spoken language):

```
COMPOSE_FILE=docker-compose.yml:docker-compose.nllb.yml
TRANSLATE_URL=http://nllb:5000
```

The first start converts the model and takes a long time. Fixed translations for NLLB go in `services/nllb/glossary/glossary.txt` (`wrong translation = correct translation`, works without a restart). Note: the NLLB-200 model weights have their own non-commercial licence (CC-BY-NC 4.0).

### `llm` – a language model

Any OpenAI-compatible chat API (`/v1/chat/completions`), for example Ollama, llama.cpp server, vLLM, LM Studio or an online service.

```
TRANSLATE_ENGINE=llm
LLM_URL=http://192.168.1.20:11434/v1
LLM_MODEL=name-of-the-model
LLM_API_KEY=
```

The lines of a piece go to the model together (more coherence). You can change the instruction for the model with `LLM_PROMPT`. A language model is usually slower than NLLB or LibreTranslate; keep `TRANSLATE_TIMEOUT`/`LLM_TIMEOUT` well below `SEGMENT_SECONDS`.

When the translator fails, the original text stays visible and the stream keeps running.

---

## Web interface

Open `http://IP-OF-THIS-PC:8088/` in a browser on your PC, tablet or phone. There are three pages:

**Status** (refreshes every 3 seconds)
- A red warning when the speech recognition or translator does not work, with the reason.
- The channel that is being translated, its state, the detected language, the buffer for Kodi and how far the subtitles are behind the speech.
- **Latest subtitles** (5): the translated text with the original underneath, newest first. Handy to read back something you missed.
- The address to put in Kodi, with a copy button.

**Subtitles**
- Up to 500 subtitles (translation with the original underneath), newest at the top, with buttons for bigger or smaller text. Handy on a tablet next to the TV. The Status page shows the latest 5.

**Channels**
- All channels per group, with a search box. The channel that is being translated now has a green border. You choose the channel in Kodi.

**Translation**
- Choose the **language of the channels** (automatic or fixed), the **subtitle language** and which languages are accepted.
- Choose the **speech recognition** (server address, model, key) and the **translator** (LibreTranslate/NLLB or a language model).
- **Test buttons** check the connection before you save. Changes work right away, without a restart.
- `command` and `plugin` can only be chosen when they are set up in `.env` (the web page cannot start programs).

**Subtitle style** (also directly at `/settings`)
- **Letters:** font, size, outline around the letters, dark box.
- **Colours:** newest and older line, show the original text.
- **Position and lines:** bottom or top, distance to the edge, number of lines, characters per line, sentences at once.
- **Timing:** reading speed, minimum and maximum time on screen, how far the subtitle may lag.

The preview uses the same drawing method as the subtitles in Kodi. **Save** works right away for new subtitles; because of the buffer you see it in Kodi after about half a minute. The values are stored in `data/settings.json` and take priority over `.env`. **Back to .env values** removes them again.

**Own fonts:** put `.ttf` or `.otf` files in `data/fonts/` and reload the page.

**Password:** set `WEB_PASSWORD=...` in `.env` when the web interface is reachable outside your own network (the user name does not matter). Kodi, the channel list and `/status` need no password.

Kodi and the browser can share the same address: a browser gets the web interface at `/`, Kodi gets the channel list. For Kodi, `/kodi.m3u` is the recommended address. Do not simply open port 8088 to the internet: the channel list and streams are reachable there too.

---

## Setting up Kodi

1. Install **PVR IPTV Simple Client** (Add-ons → Install from repository → PVR clients).
2. Add-on settings → *Location*: **Remote path (internet address)** → `http://IP-OF-THIS-PC:8088/kodi.m3u`. Turn *Cache m3u* off.
3. Subtitle on automatically: **Settings → Player → Language → Preferred subtitle language** = your target language (e.g. *Dutch*).
4. After adding or changing channels: **PVR & Live TV → General → Clear data**, and restart Kodi.

Tips for a stalling picture or crashes when zapping:
- Player → Videos (level *Advanced*): try switching hardware acceleration (VDPAU/VAAPI) off.
- **Adjust display refresh rate → On start/stop**: 25 frames per second on 50 Hz.

---

## Channels

Every list in the `channels/` folder is its own group in Kodi. Fetch lists from [iptv-org](https://github.com/iptv-org/iptv):

```bash
./scripts/fetch-channels.sh UK uk                 # country
./scripts/fetch-channels.sh Dutch lang:nld        # language
./scripts/fetch-channels.sh News news             # category
./scripts/fetch-channels.sh Spanish es ar         # several sources in one group
```

Making your own list: every channel is two lines.

```
#EXTINF:-1 lang="en",Name of the channel
https://example.invalid/live/index.m3u8
```

`lang="xx"` is only used with `USE_PLAYLIST_LANG=1`. After a change: `docker compose restart translator` and clear the data in Kodi. An example is in `examples/channels-example.m3u`. Keep the list short: not every channel works (offline, or only from within its own country). Only use streams you are allowed to watch.

---

## All settings

Everything is explained in `.env.example`. After a change: `docker compose up -d`.

### Speech recognition

| Setting | Default | What it does |
|---|---|---|
| `ASR_ENGINE` | `http` | `http`, `command` or `plugin` |
| `ASR_URL` | – | Base address of the API (usually ending in `/v1`) |
| `ASR_API_KEY` | – | Key, if the server asks for one |
| `ASR_MODEL` | – | Model name on the server (empty = default) |
| `ASR_RESPONSE_FORMAT` | `verbose_json` | `verbose_json`, `json` or `text` |
| `ASR_COMMAND` | – | Program for `command` |
| `ASR_PLUGIN` | – | Python file for `plugin` |
| `ASR_TIMEOUT` | `auto` | Seconds to wait per piece; `auto` = 60% of `SEGMENT_SECONDS` (never longer than a piece, so the picture does not stall) |
| `ASR_TRANSLATE` | `1` | The speech recognition may translate to English |
| `ASR_PROMPT_CONTEXT` | `0` | Send the previous text as context |
| `ASR_MAX_COMPRESSION_RATIO` | `2.6` | Filter: remove repeating nonsense sentences |
| `ASR_NO_SPEECH_PROB` / `ASR_MIN_AVG_LOGPROB` | `0.75` / `-1.0` | Filter: remove made-up sentences during silence |

### Translation

| Setting | Default | What it does |
|---|---|---|
| `TRANSLATE_ENGINE` | `libretranslate` | `libretranslate` or `llm` |
| `TRANSLATE_URL` | – | Address of LibreTranslate or the NLLB service |
| `TRANSLATE_API_KEY` | – | Key for LibreTranslate |
| `TRANSLATE_TIMEOUT` | `auto` | Seconds to wait; `auto` = 35% of `SEGMENT_SECONDS` |
| `LLM_URL` / `LLM_MODEL` / `LLM_API_KEY` | – | Language model (OpenAI-compatible) |
| `LLM_TIMEOUT` | `auto` | Seconds to wait for the language model; `auto` = 35% of `SEGMENT_SECONDS` |
| `LLM_PROMPT` | – | Own instruction; `{src}`, `{dst}`, `{n}` are filled in |

### Languages

| Setting | Default | What it does |
|---|---|---|
| `SOURCE_LANG` | `auto` | Detect per channel, or a fixed language code |
| `TARGET_LANG` | `nl` | Language of the subtitles |
| `USE_PLAYLIST_LANG` | `0` | `1` = use `lang="xx"` from the channel list |
| `ASR_TO_ENGLISH` | `auto` | Languages the translator does not know go to English first (`auto`/`always`/`never`) |
| `VIA_ENGLISH_LANGS` | – | These languages always via English, e.g. `sq,ta` |
| `LANGUAGE_ALLOWLIST` | – | Only accept these languages, e.g. `nl,en,fr,de,es,it,pt` |
| `LANGUAGE_ACCEPT_PROBABILITY` | `0.55` | Below this language certainty no subtitle |
| `LANGUAGE_MIN_PROBABILITY` | `0.60` | Certainty to count as a vote for fixing the language |
| `LANGUAGE_VOTES` | `5` | This many reliable pieces to fix the language |
| `LANGUAGE_RECHECK_CHUNKS` | `30` | Re-check the language after this many pieces |

The certainty limits only work when the speech recognition returns a certainty. A language change only counts after two checks in a row.

### Pieces and sentences

| Setting | Default | What it does |
|---|---|---|
| `SEGMENT_SECONDS` | `8` | Length of a piece |
| `OVERLAP_SECONDS` | `2.0` | This much audio of the previous piece goes along (sentences on the border) |
| `VAD_THRESHOLD` | `0.30` | Sensitivity of the voice detection (lower = more sensitive) |
| `MIN_SPEECH_SECONDS` | `0.3` | Less speech = silence or music: no speech recognition |
| `VAD_MUTE` | `1` | Mute parts without speech before sending |
| `MERGE_MAX_CHARS` | `200` | Translate half sentences together up to this many characters |
| `HOLD_UNFINISHED` / `HOLD_MAX_SECONDS` | `1` / `6` | Carry an unfinished sentence at the end over to the next piece |
| `OVERLAP_TRIM` / `OVERLAP_FUZZY` | `1` / `0.80` | Remove repetition caused by the overlap |

### Subtitles

These can also be changed on `/settings`.

| Setting | Default | What it does |
|---|---|---|
| `SUB_MODE` | `dvb` | `dvb` (in the stream), `track` (WebVTT, Kodi 21+), `burn` (burned in) |
| `KODI_STREAM` | `ts` | `ts` = one continuous stream, `hls` = playlist |
| `SUB_FONT_SIZE` | `54` | Font size on 1080 lines |
| `SUB_FONT_FILE` | – | Font (empty = DejaVu Sans) |
| `SUB_OUTLINE` | `8` | Outline around the letters |
| `SUB_BOX` | `0` | Dark box (0–100) |
| `SUB_NEW_COLOR` / `SUB_OLD_COLOR` | `FFFFFF` / `E6D2A0` | Colour of the newest / older line |
| `SUB_POSITION` / `SUB_MARGIN` | `bottom` / `70` | Position and distance to the edge |
| `SUB_LINES` | `1` | Sentences on screen at once |
| `SUB_MAX_LINES` | `2` | At most this many lines |
| `SUB_CHUNK_LINES` | `0` | Long sentence in parts of this many lines (0 = automatic) |
| `SUB_MAX_CHARS` | `42` | Characters per line |
| `SUB_READ_CPS` | `15` | Reading speed (characters per second) |
| `SUB_MIN_SECONDS` | `1.0` | Minimum time on screen |
| `SUB_MAX_DELAY` | `6` | At most this many seconds behind the speech |
| `SUB_HOLD` | `4` | At most this long on screen without a new sentence |
| `SHOW_ORIGINAL` | `0` | Original text small and yellow underneath |
| `WEB_PASSWORD` | – | Password for the web interface |

### Stream and buffer

| Setting | Default | What it does |
|---|---|---|
| `BUFFER_SEGMENTS` | `2` | Waiting screen until this many pieces are ready (more = fewer stalls) |
| `PROFILE_WAIT_SECONDS` | `10` | Wait for the format of a new channel (waiting screen in the same format) |
| `IDLE_STOP` | `60` | Stop after this many seconds without a viewer |
| `MAX_HEIGHT` / `MAX_HEIGHT_STRICT` | `1080` / `1` | Highest picture quality; strict = refuse a channel that only exists higher |
| `USER_AGENT` | VLC | Used to open the channel |
| `MAX_BACKLOG` | `0` | From this many waiting pieces on, temporarily no subtitles (0 = never) |
| `QUEUE_WARN_SEGMENTS` | `5` | Warning in the log from this many waiting pieces |
| `LIVE_DELAY` / `START_SEGMENTS` / `SUB_TRACK_NAME` | | Only with `SUB_MODE=track` |
| `VIDEO_ENCODER` / `VIDEO_BITRATE` / `DEINTERLACE` / `SUB_SHADOW` | | Only with `SUB_MODE=burn` |

---

## Checking and troubleshooting

- **Web interface:** `http://IP-OF-THIS-PC:8088/` – status and latest subtitles. All details as JSON: `/status`.
- **Log:** `docker compose logs -f translator`. Every minute one `Overview:` line with the number of pieces, processing time, buffer and how often the speech recognition ran (`silence` = skipped during silence).

| Problem | Solution |
|---|---|
| No subtitles, red warning on the status page | Open the page **Translation**, fill in the address and press **Test speech recognition**. Without a working speech recognition the channels play without subtitles. |
| `Speech recognition (http) returned an error` | Server not reachable, too slow or wrong model name. It is retried after 10, 20, 40, 60 s; meanwhile the picture continues without subtitles. |
| `WARNING: audio queue` | The speech recognition is slower than the channel. Use a faster model, or `MAX_BACKLOG=8`. |
| `Translator (...) not reachable` | The original text is shown; retried after 30 s. |
| `Language filter: piece not used` | Language not in `LANGUAGE_ALLOWLIST` or too uncertain. Fix the language with `lang="xx"`. |
| `BUFFER ALMOST EMPTY` | Processing or source too slow; the picture may stall. `BUFFER_SEGMENTS=3` gives more buffer. |
| Screen *Channel not reachable* | Channel offline or blocked in your country. Test the address in VLC first. |
| Kodi shows old channels | PVR & Live TV → Clear data, restart Kodi, *Cache m3u* off. |

---

## Folders

```
app/                    the program
  main.py               channels, stream, subtitles, web server
  asr.py                speech recognition connections (http, command, plugin)
  translate.py          translator connections (libretranslate, llm)
  vad.py                voice activity detection on the CPU (Silero VAD)
  dvbsub.py             drawing the subtitles (DVB)
  settings.py           web interface: subtitle style and preview
  languages.py          language codes and names
  web/index.html        the web interface (status, channels, subtitle style)
services/nllb/          optional NLLB translator (NVIDIA GPU)
scripts/                fetch channel lists, check versions
examples/               example channels, example plugin and program
channels/               your channel lists (not in git)
data/                   created at start: settings, remembered formats, fonts/ (not in git)
```

Changes per version: see [CHANGELOG.md](CHANGELOG.md).

---

## Support and licence

This project is free to use under the [MIT licence](LICENSE).

If it is useful to you, you can support the work with a sponsorship via the **Sponsor** button on GitHub.

Third-party parts keep their own licences: Silero VAD (MIT), ffmpeg (LGPL/GPL), the DejaVu, Open Sans and Liberation fonts, and – only when you use the optional NLLB service – the NLLB-200 model weights (CC-BY-NC 4.0, non-commercial).
