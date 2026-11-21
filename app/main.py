#!/usr/bin/env python3
"""
M3U Translator Subtitle - live translated subtitles for IPTV channels in Kodi.

Live IPTV channel -> speech recognition of your choice -> translator of your choice
-> subtitles inside the stream -> Kodi.

How it works:
  * A small web server (port 8088) gives Kodi a channel list (kodi.m3u),
    built from the lists in the channels/ folder.
  * When Kodi opens a channel, translation starts for that channel
    (and the previous one stops). While starting, Kodi shows a waiting screen.
  * Per channel:
      1. ffmpeg cuts the live stream into pieces of SEGMENT_SECONDS seconds.
      2. Per piece: voice activity detection (CPU) -> speech recognition (ASR_ENGINE)
         -> translation (TRANSLATE_ENGINE).
      3. SUB_MODE=dvb (default): video and audio untouched, subtitles as DVB
         subtitles inside the stream (like digital TV). On/off in Kodi.
         SUB_MODE=track: WebVTT track via inputstream.adaptive (Kodi 21+).
         SUB_MODE=burn: subtitles burned into the picture (re-encoding).
      4. Kodi gets one continuous stream per channel.
  * When nobody is watching any more, translation stops by itself.
  * /settings: web page to change the subtitle style live.
This program contains no speech model itself: see asr.py.
"""

import bisect
import http.server
import json
import math
import os
import queue
import re
import shutil
import socketserver
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import deque

VERSION = "V4.3"


# ---------------------------------------------------------------- settings
def env(name, default=None, *old_names):
    """Value from the environment. old_names: older names that still work."""
    for n in (name,) + old_names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    return default


def language_allowlist():
    """Empty/all/* means no allowlist; otherwise a set of ISO language codes."""
    raw = str(env("LANGUAGE_ALLOWLIST", "") or "").strip().lower()
    if raw in ("", "all", "*"):
        return None
    values = {x.strip() for x in raw.split(",") if x.strip()}
    return values or None


CFG = {
    "stream_url": env("STREAM_URL"),                       # old: a single channel
    "channels_file": env("CHANNELS_FILE", "/config/channels.m3u"),
    "channels_dir": env("CHANNELS_DIR", "/config/channels"),
    "port": int(env("PORT", "8088")),
    "idle_stop": int(env("IDLE_STOP", "60")),              # stop after x s without viewer
    "max_height": max(0, int(env("MAX_HEIGHT", "1080"))),
    # Refuse streams that only exist above MAX_HEIGHT (e.g. only 4K).
    # An unknown resolution is always opened.
    "max_height_strict": env("MAX_HEIGHT_STRICT", "1") == "1",
    "user_agent": env("USER_AGENT", "VLC/3.0.20 LibVLC/3.0.20"),

    # --- speech recognition (see asr.py) ---
    # http = OpenAI-compatible server, command = own program, plugin = own Python file
    "asr_engine": env("ASR_ENGINE", "http").strip().lower(),
    "asr_url": env("ASR_URL", ""),
    "asr_key": env("ASR_API_KEY", ""),
    "asr_model": env("ASR_MODEL", ""),
    "asr_format": env("ASR_RESPONSE_FORMAT", "verbose_json").strip().lower(),
    "asr_command": env("ASR_COMMAND", ""),
    "asr_plugin": env("ASR_PLUGIN", ""),
    # seconds per piece; "auto" = 60% of SEGMENT_SECONDS, so the stream never waits too long
    "asr_timeout": env("ASR_TIMEOUT", "auto"),
    # 1 = the speech recognition can also translate to English (task=translate)
    "asr_translate": env("ASR_TRANSLATE", "1") == "1",
    # 1 = send the previous text as 'prompt' (context); can cause repetitions
    "context": env("ASR_PROMPT_CONTEXT", "0", "WHISPER_CONTEXT") == "1",
    # Filters against made-up text; only used when the server returns these values.
    "compression_ratio": float(env("ASR_MAX_COMPRESSION_RATIO", "2.6", "WHISPER_COMPRESSION_RATIO")),
    "no_speech_prob": float(env("ASR_NO_SPEECH_PROB", "0.75", "WHISPER_NO_SPEECH_PROB")),
    "avg_logprob": float(env("ASR_MIN_AVG_LOGPROB", "-1.0", "WHISPER_AVG_LOGPROB")),

    # --- translation (see translate.py) ---
    # libretranslate = LibreTranslate API (also the NLLB service), llm = OpenAI-compatible chat API
    "tr_engine": env("TRANSLATE_ENGINE", "libretranslate").strip().lower(),
    "lt_url": (env("TRANSLATE_URL", "", "LIBRETRANSLATE_URL") or "").rstrip("/"),
    "lt_key": env("TRANSLATE_API_KEY", "", "LIBRETRANSLATE_API_KEY"),
    "lt_timeout": env("TRANSLATE_TIMEOUT", "auto", "LIBRETRANSLATE_TIMEOUT"),
    "llm_url": (env("LLM_URL", "") or "").rstrip("/"),
    "llm_key": env("LLM_API_KEY", ""),
    "llm_model": env("LLM_MODEL", ""),
    "llm_timeout": env("LLM_TIMEOUT", "auto"),
    "llm_prompt": env("LLM_PROMPT", ""),

    # --- languages ---
    "src_lang": env("SOURCE_LANG", "auto"),
    # 0 = always detect the language; 1 = respect lang="xx" from the M3U
    "use_playlist_lang": env("USE_PLAYLIST_LANG", "0") == "1",
    "dst_lang": env("TARGET_LANG", "nl"),
    # via_en: auto = go via English only when the translator does not know the language
    #         always = always via English,  never = never
    "via_en": env("ASR_TO_ENGLISH", "auto", "WHISPER_TO_ENGLISH"),
    # these languages always go via English first (comma separated), e.g. "sq,ku"
    "via_en_langs": {x.strip().lower() for x in env("VIA_ENGLISH_LANGS", "").split(",") if x.strip()},
    "lang_recheck": max(0, int(env("LANGUAGE_RECHECK_CHUNKS", "30"))),
    # LANGUAGE_MIN_PROBABILITY: certainty for a vote that fixes the channel language.
    "lang_probability": min(1.0, max(0.0, float(env("LANGUAGE_MIN_PROBABILITY", "0.60")))),
    # LANGUAGE_ACCEPT_PROBABILITY: lower limit; uncertain automatic detections are not shown.
    "lang_accept_probability": min(1.0, max(0.0, float(env("LANGUAGE_ACCEPT_PROBABILITY", "0.55")))),
    # Empty, all or * = allow all languages. A list such as nl,en,fr limits detection.
    "lang_allowlist": language_allowlist(),
    "lang_votes": max(3, int(env("LANGUAGE_VOTES", "5"))),

    # --- pieces, sentences and voice activity detection ---
    "seg_seconds": int(env("SEGMENT_SECONDS", "8")),
    "overlap": float(env("OVERLAP_SECONDS", "2.0")),        # extra context around piece borders
    "vad": float(env("VAD_THRESHOLD", "0.30")),             # lower = more sensitive to speech
    # Less speech than this (seconds) in a piece = silence or music: no speech recognition.
    "min_speech": max(0.0, float(env("MIN_SPEECH_SECONDS", "0.3"))),
    # 1 = mute the parts without speech before sending (less made-up text)
    "vad_mute": env("VAD_MUTE", "1") == "1",
    # From this many waiting pieces on, no new subtitles for that piece
    # (video keeps running), so a backlog can catch up. 0 = never.
    "max_backlog": int(env("MAX_BACKLOG", "0")),
    # Merge short pieces without sentence end into one sentence before translating. 0 = off.
    "merge_chars": int(env("MERGE_MAX_CHARS", "200")),
    # When the last sentence continues into the next piece, do not show it yet but
    # pass its audio on to the next piece (then translated once, complete).
    "hold_unfinished": env("HOLD_UNFINISHED", "1") == "1",
    "hold_max": float(env("HOLD_MAX_SECONDS", "6")),     # spoken longer: show anyway
    "overlap_trim": env("OVERLAP_TRIM", "1") == "1",        # remove repetition caused by overlap
    "overlap_fuzzy": float(env("OVERLAP_FUZZY", "0.80")),    # 0.80 = 80% equal letters = repetition
    "ingest_list_size": int(env("INGEST_LIST_SIZE", "45")),
    "ingest_delete_threshold": int(env("INGEST_DELETE_THRESHOLD", "15")),
    "queue_warn_segments": int(env("QUEUE_WARN_SEGMENTS", "5")),

    # --- subtitles (style can also be changed live on /settings) ---
    "lines": int(env("SUB_LINES", "1")),                    # sentences on screen at once (roll-up)
    "hold": float(env("SUB_HOLD", "4")),                    # max. seconds on screen without new sentence
    "font": env("SUB_FONT", "DejaVu Sans"),                 # only SUB_MODE=burn
    "font_file": env("SUB_FONT_FILE", ""),                  # empty = DejaVu Sans
    "font_size": int(env("SUB_FONT_SIZE", "54")),           # on 1080 lines
    "show_original": env("SHOW_ORIGINAL", "0") == "1",      # original small underneath
    "new_color": env("SUB_NEW_COLOR", "FFFFFF"),            # newest sentence (RGB, hex)
    "old_color": env("SUB_OLD_COLOR", "E6D2A0"),            # older sentence above (beige)
    "position": env("SUB_POSITION", "bottom"),              # bottom or top
    "margin": int(env("SUB_MARGIN", "70")),                 # distance to the edge (on 1080)
    "box": int(env("SUB_BOX", "0")),                        # dark box: 0 = none, 100 = black
    "shadow": int(env("SUB_SHADOW", "0")),                  # shadow depth (burn only)
    "outline": int(env("SUB_OUTLINE", "8")),                # outline around the letters
    # dvb = DVB subtitle in the stream, track = WebVTT (Kodi 21+), burn = burn in
    "sub_mode": env("SUB_MODE", "dvb").lower(),
    # ts = one continuous stream to Kodi (language label visible), hls = playlist
    "kodi_stream": env("KODI_STREAM", "ts").lower(),
    "sub_max_lines": int(env("SUB_MAX_LINES", "2")),        # at most this many text lines at once
    # Long sentences in parts of this many lines after each other. 0 = automatic.
    "chunk_lines": int(env("SUB_CHUNK_LINES", "0")),
    # At most this many characters per line (subtitling guideline: 42). 0 = screen width only.
    "sub_max_chars": int(env("SUB_MAX_CHARS", "42")),
    # Reading speed (characters per second). Lower = longer on screen, but further behind speech.
    "read_cps": max(4.0, float(env("SUB_READ_CPS", "15"))),
    "read_min": max(0.5, float(env("SUB_MIN_SECONDS", "1.0"))),    # also for a short word
    # With fast speech the subtitle may lag this many seconds; it catches up in pauses.
    "read_max_delay": max(0.0, float(env("SUB_MAX_DELAY", "6"))),
    "sub_lang_name": env("SUB_TRACK_NAME", "Dutch (translated)"),

    # --- stream and buffer ---
    "work": env("WORK_DIR", "/work"),
    "out": env("OUT_DIR", "/out"),
    "window": int(env("OUT_WINDOW", "10")),
    # SUB_MODE=track only: start this many seconds behind the live edge.
    "live_delay": int(env("LIVE_DELAY", "24")),
    "start_segments": int(env("START_SEGMENTS", "3")),
    # Waiting screen stays until this many translated pieces are ready (more = fewer stalls).
    "buffer_segments": max(1, int(env("BUFFER_SEGMENTS", "2"))),
    # Folder for remembered channel formats, waiting screens, settings and own fonts.
    "model_dir": env("DATA_DIR", "/data", "MODEL_DIR"),
    # For a new channel wait this long for its format (codecs), so the waiting
    # screen has exactly the same format. Known channel: max. 3 s.
    "profile_wait": max(0.0, float(env("PROFILE_WAIT_SECONDS", "10"))),
    # SUB_MODE=burn only
    "encoder": env("VIDEO_ENCODER", "libx264"),
    "bitrate": env("VIDEO_BITRATE", "6M"),
    "deinterlace": env("DEINTERLACE", "1") == "1",

    # --- web page /settings ---
    "web_password": env("WEB_PASSWORD", ""),
}



def _timeout(value, share, minimum):
    """'auto' or empty -> a share of SEGMENT_SECONDS; a number is used as given (capped
    to the piece length, otherwise the stream for Kodi would wait and stall)."""
    limit = max(minimum, CFG["seg_seconds"] * share)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return limit
    return max(1.0, min(v, CFG["seg_seconds"] * 0.9))


CFG["asr_timeout"] = _timeout(CFG["asr_timeout"], 0.6, 3.0)
CFG["lt_timeout"] = _timeout(CFG["lt_timeout"], 0.35, 2.0)
CFG["llm_timeout"] = _timeout(CFG["llm_timeout"], 0.35, 2.0)

STOP = threading.Event()

# Latest subtitles for the web interface (all channels, survives zapping).
RECENT = deque(maxlen=500)
_RECENT_LOCK = threading.Lock()
_RECENT_ID = [0]


def add_recent(channel, original, text, language):
    with _RECENT_LOCK:
        _RECENT_ID[0] += 1
        RECENT.append({"id": _RECENT_ID[0], "time": time.strftime("%H:%M:%S"),
                       "channel": channel, "original": original, "text": text,
                       "language": language})


def recent_after(after=0, limit=500):
    """Subtitles with an id above 'after', oldest first, at most 'limit'."""
    with _RECENT_LOCK:
        items = [r for r in RECENT if r["id"] > after]
    return items[-limit:]
PLACEHOLDER = os.path.join(CFG["work"], "waiting.ts")
OFFLINE = os.path.join(CFG["work"], "offline.ts")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- helpers
def probe(path):
    """Returns (start_time, duration) in seconds of a .ts file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=start_time,duration",
         "-of", "json", path],
        capture_output=True, text=True, timeout=30)
    fmt = json.loads(out.stdout or "{}").get("format", {})
    return float(fmt.get("start_time", 0) or 0), float(fmt.get("duration", 0) or 0)


def probe_av(path):
    """v0.31: (start, duration, audio start) of a .ts piece.
    The speech recognition counts from the first audio; that can start slightly after the video."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=start_time,duration:stream=codec_type,start_time", "-of", "json", path],
        capture_output=True, text=True, timeout=30)
    info = json.loads(out.stdout or "{}")
    fmt = info.get("format", {})
    start = float(fmt.get("start_time", 0) or 0)
    dur = float(fmt.get("duration", 0) or 0)
    a_start = start
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            try:
                a_start = float(s["start_time"])
            except (KeyError, TypeError, ValueError):
                pass
            break
    return start, dur, a_start


def frames_duration(path):
    """v0.40: duration from the number of frames (when the times in the file are wrong)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_packets,avg_frame_rate,r_frame_rate",
             "-of", "json", path], capture_output=True, text=True, timeout=30).stdout
        st = (json.loads(out or "{}").get("streams") or [{}])[0]
        fps = fps_value(st.get("avg_frame_rate"))
        if not 8.0 <= fps <= 61.0:
            fps = fps_value(st.get("r_frame_rate"))
        n = int(st.get("nb_read_packets") or 0)
        return n / fps if n and 8.0 <= fps <= 61.0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def probe_sane(path):
    """v0.40: probe_av, but with a check for nonsense times. After ~26.5 hours the
    timestamps of every channel wrap around (33-bit PTS), and a stalling source can give odd times.
    A piece of '95378 s' made Kodi's stream wait for hours (tested).
    Returns (start, duration, audio start, corrected)."""
    start, dur, a_start = probe_av(path)
    fixed = False
    limit = 3.0 * CFG["seg_seconds"]
    if not 0.0 < dur <= limit:
        dur = min(max(frames_duration(path) or float(CFG["seg_seconds"]), 0.1), limit)
        fixed = True
    if abs(a_start - start) > 5.0:
        a_start = start
        fixed = True
    return start, dur, a_start, fixed


def gpu_now():
    """v0.31: GPU load right now (for /status and the per-minute overview)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,clocks.sm,"
             "clocks.max.sm,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        u, t, c, cmax, mu, mt = [x.strip() for x in out[0].split(",")[:6]]
        return {"load_pct": int(u), "temp_c": int(t), "clock_mhz": int(c),
                "clock_max_mhz": int(cmax), "memory_mb": f"{mu}/{mt}"}
    except Exception:  # noqa: BLE001
        return None


def ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_alpha(percent):
    """opacity in % -> ASS alpha (00 = opaque, FF = invisible)"""
    p = max(0, min(100, percent))
    return f"{round(255 * (1 - p / 100)):02X}"


def ass_escape(text):
    return (text.replace("\\", "/").replace("{", "(").replace("}", ")")
            .replace("\n", " ").strip())


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: NL,{font},{size},&H00FFFFFF,&H00FFFFFF,&H{oc}000000,&H{box}000000,-1,0,0,0,100,100,0,0,{bs},{outline},{shadow},{align},120,120,{margin},1
Style: EN,{font},{small},&H0000E5FF,&H0000E5FF,&H{oc}000000,&H{box}000000,0,0,0,0,100,100,0,0,{bs},{outline},{shadow},{align},120,120,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# ---------------------------------------------------------------- speech
# 'Languages' that (Whisper-like) speech servers report for music or noise (known: 'nn' = Nynorsk).
# Such a report is no real language change: do not re-check every following piece.
NOISE_LANGS = {"nn", "haw", "sn", "ln", "jw", "la"}
NOISE_SPEECH = 1.0          # less speech than this (s): treat a language report as noise
VAD_PAD = 0.6               # margin around speech when muting the rest (s)


def speech_regions(audio):
    """Speech regions [(start, end)] in seconds (voice activity detection on the CPU)."""
    from vad import speech_regions as _regions
    return _regions(audio, threshold=CFG["vad"], min_silence=0.35, pad=0.0)


class Transcriber:
    """Speech -> text with the chosen speech recognition (ASR_ENGINE), plus everything
    that is independent of it: voice activity detection, language detection per channel
    (votes, allowlist, certainty, confirmed language change) and filters against made-up text."""

    def __init__(self, engine=None):
        if engine is None:
            from asr import make_engine
            engine = make_engine(CFG)
        self.engine = engine
        # One lock around the speech recognition: while zapping the old channel can
        # still be busy while the new one starts; this way they never mix.
        self.lock = threading.RLock()
        self.prev_text = ""
        self.fixed_lang = None
        self.votes = deque(maxlen=max(3, CFG["lang_votes"]))
        self.lang_mode = CFG["src_lang"]
        self.calls = 0
        self.pending = None          # possible language change, waiting for confirmation
        self.force_recheck = False
        self.last_language = None
        self.last_language_probability = 0.0
        self.last_language_guard_log = 0.0
        self.last_language_guard_reason = ""
        self.stats = {"asr": 0, "silence": 0, "errors": 0}
        self.down_until = 0.0
        self.last_error_log = 0.0
        self.fails = 0               # errors in a row (for the pause before trying again)
        self.last_error = None       # (time, text) of the last error, for the web interface
        self.no_prob_logged = False

    def reset(self):
        self.prev_text = ""

    def set_channel(self, lang):
        """New channel: detect automatically, unless an override is allowed."""
        with self.lock:
            self.reset()
            self.lang_mode = lang or CFG["src_lang"]
            self.fixed_lang = None
            self.votes.clear()
            self.calls = 0
            self.pending = None
            self.force_recheck = False
            self.last_language = None
            self.last_language_probability = 0.0
            self.last_language_guard_log = 0.0
            self.last_language_guard_reason = ""

    def known_lang(self):
        """Language of the channel when it has been reliably established."""
        if self.lang_mode == "auto":
            return self.fixed_lang
        return self.lang_mode

    def _language_guard_reason(self, language, probability):
        """Why an automatic language detection is not safe enough to show."""
        if not language:
            return "no language code received"
        if probability < CFG["lang_accept_probability"]:
            return (f"low language certainty {probability:.2f} "
                    f"(< {CFG['lang_accept_probability']:.2f})")
        allowed = CFG["lang_allowlist"]
        if allowed is not None and language.lower() not in allowed:
            return f"language '{language}' is not in LANGUAGE_ALLOWLIST"
        return None

    def _log_language_guard(self, reason):
        """Limit repeated log lines during silence or a language that is not allowed."""
        now = time.monotonic()
        if reason != self.last_language_guard_reason or now - self.last_language_guard_log >= 30:
            log(f"Language filter: piece not used ({reason})")
            self.last_language_guard_reason = reason
            self.last_language_guard_log = now

    def transcribe(self, audio, task="transcribe", force_lang=None, vad=True):
        """Audio is numpy float32, 16 kHz mono. Returns (segments, language)."""
        with self.lock:
            return self._transcribe(audio, task, force_lang, vad)

    def _count(self, key):
        self.stats[key] = self.stats.get(key, 0) + 1

    def _speech(self, audio):
        """Speech in this piece according to voice activity detection (CPU, fast), or None."""
        try:
            return speech_regions(audio)
        except Exception as e:  # noqa: BLE001
            log(f"Voice activity detection error ({e}); piece goes to speech recognition anyway")
            return None

    def _recognize(self, audio, task, lang, speech):
        """Calls the speech recognition. Returns (segments, language, certainty) or None."""
        from asr import ASRUnsupported, normalize
        if time.time() < self.down_until:
            return None
        send = audio
        if speech is not None and CFG["vad_mute"]:
            from vad import keep_speech
            dur = len(audio) / 16000.0
            send = keep_speech(audio, [(max(0.0, a - VAD_PAD), min(dur, b + VAD_PAD))
                                       for a, b in speech])
        prompt = (self.prev_text[-200:] or None) if CFG["context"] else None
        self._count("asr")
        try:
            res = self.engine.recognize(send, task=task, language=lang, prompt=prompt)
            span = (speech[0][0], speech[-1][1]) if speech else None
            out = normalize(res, len(audio) / 16000.0, span)
            self.fails = 0
            self.last_error = None
            return out
        except ASRUnsupported:
            raise
        except Exception as e:  # noqa: BLE001
            # Wait longer after every error in a row (10, 20, 40, 60 s): a server that is
            # down must not make every piece wait for a timeout (the picture would stall).
            self._count("errors")
            self.fails += 1
            pause = min(60, 10 * 2 ** (self.fails - 1))
            self.down_until = time.time() + pause
            self.last_error = (time.strftime("%H:%M:%S"), str(e)[:200])
            if time.time() - self.last_error_log > 30:
                self.last_error_log = time.time()
                log(f"Speech recognition ({self.engine.name}) returned an error: {e} "
                    f"- no new subtitles for {pause} s, then trying again")
            return None

    def _filter(self, segs):
        out = []
        for seg in segs:
            text = dedupe(seg["text"].strip())
            if not text or is_junk(text):
                continue
            if seg.get("compression_ratio", 0.0) > CFG["compression_ratio"]:
                continue
            nsp = seg.get("no_speech_prob")
            if (nsp is not None and nsp > CFG["no_speech_prob"]
                    and seg.get("avg_logprob", 0.0) < CFG["avg_logprob"]):
                continue
            # typical made-up text during silence/music ("Thank you.")
            if is_filler(text) and (nsp is None or nsp > 0.3):
                continue
            out.append((float(seg["start"]), float(seg["end"]), text))
        return out

    def _transcribe(self, audio, task, force_lang, vad, speech=None):
        self.calls += 1
        recheck = False
        if self.lang_mode == "auto":
            # Detect every piece at the start. After fixing the language re-check periodically,
            # so that a language change or a wrong first guess does not stick.
            recheck = (self.fixed_lang is None or CFG["lang_recheck"] <= 0 or
                       self.force_recheck or self.calls % CFG["lang_recheck"] == 0)
            lang = None if recheck else self.fixed_lang
        else:
            lang = self.lang_mode
        if force_lang:
            lang = force_lang
            recheck = False

        # First (on the CPU) check whether anyone is speaking
        if vad and speech is None:
            speech = self._speech(audio)
        talk = None if speech is None else sum(b - a for a, b in speech)

        # Silence or music -> no speech recognition (saves work and made-up text)
        if talk is not None and talk < CFG["min_speech"]:
            self._count("silence")
            if recheck and not force_lang and self.fixed_lang:
                self.calls -= 1          # postpone the language check until there is speech again
            if task == "translate":
                return [], "en"
            return [], (lang or self.fixed_lang or "und")

        from asr import ASRUnsupported
        try:
            got = self._recognize(audio, task, lang, speech if vad else None)
        except ASRUnsupported as e:
            # Translating to English is not possible: just transcribe in the own language.
            if not getattr(self, "translate_warned", False):
                self.translate_warned = True
                log(f"Speech recognition: {e}; text stays in its own language")
            return self._transcribe(audio, "transcribe", force_lang or lang, vad, speech)
        if got is None:
            if recheck and not force_lang and self.fixed_lang:
                self.calls -= 1
            return [], ("en" if task == "translate" else (lang or self.fixed_lang or "und"))
        segs, info_lang, probability = got
        if info_lang is None and lang:
            info_lang = lang
        if probability is None:
            # The server gives no language certainty: a reported language counts as certain.
            if not self.no_prob_logged and lang is None and info_lang:
                self.no_prob_logged = True
                log("Speech recognition gives no language certainty; LANGUAGE_ACCEPT_PROBABILITY "
                    "and LANGUAGE_MIN_PROBABILITY are not used (allowlist and votes are)")
            probability = 1.0 if info_lang else 0.0
        self.last_language = info_lang
        self.last_language_probability = probability
        out = self._filter(segs)

        if task == "translate":
            return out, "en"

        if self.lang_mode == "auto" and not force_lang:
            # Only filter automatic detections. An explicitly set language or
            # a checked retry with force_lang is not blocked.
            guard_reason = self._language_guard_reason(info_lang, probability)

            if self.fixed_lang and recheck:
                self.force_recheck = False
                if guard_reason:
                    self.pending = None
                    self._log_language_guard(guard_reason)
                    allowed = CFG["lang_allowlist"]
                    detected_but_blocked = (
                        bool(info_lang) and probability >= CFG["lang_accept_probability"]
                        and allowed is not None and info_lang.lower() not in allowed
                    )
                    if detected_but_blocked:
                        if info_lang.lower() in NOISE_LANGS or (
                                talk is not None and talk < NOISE_SPEECH):
                            # Music or noise ('nn'). No real language change: skip this
                            # piece, but do NOT re-check every following piece.
                            self._log_language_guard(guard_reason + " - probably music")
                            return [], self.fixed_lang
                        # Do not force a clearly detected but disallowed language
                        # into the old language: that can produce made-up text.
                        # Keep re-checking the following pieces.
                        self.force_recheck = True
                        return [], "und"
                    # Low certainty: the existing channel language is more reliable.
                    self.calls -= 1
                    return self._transcribe(audio, task, self.fixed_lang, vad, speech)

                if info_lang == self.fixed_lang:
                    self.pending = None
                elif out and probability >= max(0.75, CFG["lang_probability"]):
                    if self.pending == info_lang:
                        # Two consecutive checks confirm the language change.
                        log(f"[{self.fixed_lang} -> {info_lang}] language changed "
                            f"(confirmed twice, certainty {probability:.2f})")
                        self.fixed_lang = info_lang
                        self.pending = None
                        self.votes.clear()
                    else:
                        # First observation: do not show this piece in a possibly wrong
                        # language. The next audio is checked again right away.
                        self.pending = info_lang
                        self.force_recheck = True
                        log(f"Possible language change {self.fixed_lang} -> {info_lang} "
                            f"(first check {probability:.2f}); piece skipped, "
                            "next piece confirms")
                        return [], "und"
                else:
                    # No convincing language change: use the last confirmed language.
                    self.pending = None
                    self.calls -= 1
                    return self._transcribe(audio, task, self.fixed_lang, vad, speech)

            elif self.fixed_lang is None:
                if guard_reason:
                    # Do not fix a language and do not translate uncertain/unwanted text.
                    self._log_language_guard(guard_reason)
                    return [], "und"

                if out and probability >= CFG["lang_probability"]:
                    self.votes.append(info_lang)
                    total = self.votes.maxlen
                    needed = max(2, (total * 4 + 4) // 5)        # ~80% of the votes
                    candidate = max(set(self.votes), key=self.votes.count)
                    if len(self.votes) >= total and self.votes.count(candidate) >= needed:
                        self.fixed_lang = candidate
                        log(f"Language detected and fixed: {candidate} "
                            f"({self.votes.count(candidate)}/{total} votes)")
        if out:
            self.prev_text = (self.prev_text + " " + " ".join(t for _, _, t in out))[-400:]
        # always the language in which this piece was actually transcribed
        return out, (lang or info_lang or "en")


JUNK = [
    "subscribe", "bell icon", "like and share", "thanks for watching", "thank you for watching",
    "enjoyed this video", "next video", "more videos", "in the comments", "see you next time",
    "amara.org", "subtítulos", "suscríbete", "suscribete", "gracias por ver",
    "sous-titres", "untertitel", "abonneer", "ondertiteling", "www.", ".com",
    "♪", "[music]", "(music)", "[applause]", "(applause)", "[laughter]", "[muziek]", "(muziek)",
]

FILLERS = {"thank you", "thank you very much", "thanks", "bye", "you", "okay", "oh",
           "dank je", "bedankt", "merci", "danke", "gracias"}


def is_filler(text):
    return text.lower().strip(" .!?,") in FILLERS


def is_junk(text):
    t = text.lower()
    if not re.search(r"\w", t):                 # v0.32: no letter or digit ('***', '...', '♪♪')
        return True
    return any(j in t for j in JUNK)


def dedupe(text):
    """'We are here. We are here. We are here.' -> 'We are here.'"""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    def norm(x):
        x = x.lower().strip(" .!?,")
        return re.sub(r"^(and|so|but|then|en|dus|maar)\s+", "", x)
    out, seen = [], set()
    for p in parts:
        k = norm(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return " ".join(out)


def _words(text):
    return re.findall(r"\w+", text.lower())


def similar(old, new):
    """Is 'old' a (cut-off) version of 'new'? E.g. 'The prime minister said'
    and 'The prime minister said today that ...'."""
    a, b = _words(old), set(_words(new))
    if len(a) < 2 or not b:
        return False
    return sum(w in b for w in a) / len(a) >= 0.7


_WORD = re.compile(r"\w+(?:'\w+)?")
_STOP = {"the", "a", "an", "and", "to", "of", "in", "on", "is", "it", "i", "so", "but",
         "de", "het", "een", "en", "van", "in", "op", "is", "ik", "dus", "maar"}


def _same_word(a, b):
    """v0.21: words the speech recognition may hear slightly differently the second time:
    'structure'/'infrastructure', 'releases'/'released'."""
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    if len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5]:
        return True
    # v0.23: same word, different spelling ('ëshë'/'është', 'shenë'/'shemë')
    if len(a) >= 4 and len(b) >= 4:
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio() >= 0.75
    return False


def _match(old_tail, new_head):
    """Do the words match? From 4 words on, 1 in 5 may differ
    (e.g. '1970' versus '1978'), but the first and last word must match."""
    same = [_same_word(a, b) for a, b in zip(old_tail, new_head)]
    bad = same.count(False)
    if bad == 0:
        return True
    return (len(same) >= 4 and same[0] and bad <= max(1, len(same) // 5)
            and (same[-1] or len(same) >= 5))


def trim_overlap(prev_text, text):
    """v0.20/v0.21: removes the part that was already in the previous sentence (because of the overlap).
    Examples:
      'Every component is built right here.' + 'built right here. The engine ...'
          -> 'The engine ...'
      '...Africa Mercy is also a floating community.' + 'She is also a floating
          community, 480 crew ...' -> '480 crew ...'   (first word may differ)
      '...charity that started in 1970.' + 'charity that started in 1978. The
          organization ...' -> 'The organization ...'  (one word may differ)
    Returns (rest, number of removed words)."""
    old = [w.lower() for w in _WORD.findall(prev_text)][-30:]
    spans = [(m.group(0).lower(), m.end()) for m in _WORD.finditer(text)]
    if not old or not spans:
        return text, 0
    new = [w for w, _ in spans]
    cut = 0
    for skip in (0, 1, 2, 3):                # v0.21/v0.22: first words may differ
        for k in range(min(len(old), len(new) - skip, 25), 0, -1):
            if k < 2 and skip:
                break
            if _match(old[-k:], new[skip:skip + k]):
                if k == 1 and (new[0] in _STOP or len(new[0]) < 3):
                    continue
                if skip and k < 3 + 2 * (skip - 1):   # the more skipped, the longer the match
                    continue
                cut = skip + k
                break
        if cut:
            break
    if not cut:
        cut = _fuzzy_cut(old, new)
    if not cut:
        return text, 0
    rest = text[spans[cut - 1][1]:].lstrip(" .,;:!?…-—\"'")
    return rest, cut


def _fuzzy_cut(old, new):
    """v0.23: compare letter by letter, for languages the speech recognition understands
    less well and therefore spells differently every time. Example (Albanian):
    'të dimojme, të punojme për vend' + 'dihmojmë dhe të punojmë për vendin tonë'
    -> only 'tonë' remains. Returns the number of words to remove (or 0)."""
    from difflib import SequenceMatcher
    best = 0
    for j in range(min(len(new), 25), 1, -1):
        a = " ".join(new[:j])
        if len(a) < 12:
            break
        for t in (j, j - 1, j + 1):
            if t < 2 or t > len(old):
                continue
            b = " ".join(old[-t:])
            if SequenceMatcher(None, a, b).ratio() >= CFG["overlap_fuzzy"]:
                best = j
                break
        if best:
            break
    return best


def same_start(old, new):
    """v0.22: do both sentences start with the same two words?"""
    a, b = _words(old)[:2], _words(new)[:2]
    return len(a) == 2 and len(b) == 2 and all(_same_word(x, y) for x, y in zip(a, b))


def split_unfinished(parts, full_len, off):
    """v0.33: is the last sentence not finished (no full stop, or ends with '...') and does
    the speech continue up to the end of the piece? Then take that sentence off.
    Returns (remaining parts, start time of the half sentence or None, its text).
    Before: first 'Fair Player is built...' and then the whole sentence again, or a
    loose tail ('bottleneck.') that was translated separately (and badly)."""
    if not CFG["hold_unfinished"] or not parts:
        return parts, None, ""
    s, e, text = parts[-1]
    t = text.rstrip()
    finished = SENTENCE_END.search(t) and not t.endswith(("...", "…"))
    # Speech runs until (almost) the end of the piece: then the sentence is probably not
    # finished, even if the speech recognition adds a full stop ('Her top speed is 17.' + 'knots, but...').
    talking_at_end = e >= full_len - 0.5
    if (finished and not talking_at_end) or e < full_len - 1.5:
        return parts, None, ""
    pieces = re.split(r"(?<=[.!?…])\s+", t)
    last = pieces[-1]
    head = t[:len(t) - len(last)].rstrip()
    hold_at = s + (e - s) * (len(head) / max(1, len(t)))
    if hold_at < off or full_len - hold_at > CFG["hold_max"]:
        return parts, None, ""        # too long or started earlier: just show it
    rest = parts[:-1] + ([(s, hold_at, head)] if head else [])
    return rest, hold_at, last


def is_shouting(text):
    """v0.33: WHOLE SENTENCE IN CAPITALS (comes from the speech recognition on some channels)."""
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.8


def clean_dst(orig, dst):
    """v0.33: the translator sometimes adds a leading dash ('- Again.') that was not there."""
    if dst[:2] in ("- ", "– ") and not orig.lstrip().startswith(("-", "–")):
        dst = dst[2:].lstrip()
        dst = dst[:1].upper() + dst[1:] if orig[:1].isupper() else dst
    return dst


def fix_case(orig, dst):
    """v0.22: when the sentence continues from the previous piece (starts with a lowercase letter),
    let the translation start with a lowercase letter too ('Assembly line.' -> 'assembly line.')."""
    if not orig or not dst or not orig[0].islower() or not dst[0].isupper():
        return dst
    first = re.match(r"\w+", dst)
    if first and first.group(0) in orig:         # proper noun as in the original
        return dst
    return dst[0].lower() + dst[1:]


def contained(new, old):
    """v0.20: is the new sentence (almost) completely in the previous one? Then it is a repetition."""
    a, b = _words(new), set(_words(old))
    if len(a) < 2 or not b:
        return False
    return sum(w in b for w in a) / len(a) >= 0.9


SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")


def merge_parts(parts):
    """Merge short pieces without a sentence end into one sentence, so that
    the translator gets whole sentences instead of loose half sentences."""
    if CFG["merge_chars"] <= 0:
        return parts
    out = []
    for s, e, t in parts:
        if (out and not SENTENCE_END.search(out[-1][2]) and s - out[-1][1] < 1.0
                and len(out[-1][2]) + 1 + len(t) <= CFG["merge_chars"]):
            out[-1] = (out[-1][0], e, out[-1][2] + " " + t)
        else:
            out.append((s, e, t))
    return out


def read_audio(path):
    import numpy as np
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "1",
         "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        return None
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


# ---------------------------------------------------------------- channels
def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "channel"


ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def clean_name(name):
    return re.sub(r'["\r\n]', "", name).strip() or "Channel"


def read_m3u(path, group):
    """Reads one m3u file (also lists from iptv-org).
    Optional per channel: lang="es", tvg-logo, http-user-agent, http-referrer."""
    chans = []
    cur = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#EXTINF"):
                attrs = dict(ATTR_RE.findall(line))
                rest = ATTR_RE.sub("", line)          # remove attributes (with commas)
                name = rest.split(",", 1)[1] if "," in rest else "Channel"
                lang = attrs.get("lang") or attrs.get("tvg-language") or ""
                cur = {"name": clean_name(name), "group": group,
                       "lang": lang.lower() if re.fullmatch(r"[a-zA-Z]{2}", lang) else None,
                       "logo": attrs.get("tvg-logo", ""),
                       "ua": attrs.get("http-user-agent", ""),
                       "referer": attrs.get("http-referrer", "")}
            elif line.startswith("#EXTVLCOPT:") and cur is not None:
                opt = line[len("#EXTVLCOPT:"):]
                if opt.startswith("http-user-agent="):
                    cur["ua"] = opt.split("=", 1)[1]
                elif opt.startswith("http-referrer="):
                    cur["referer"] = opt.split("=", 1)[1]
            elif line and not line.startswith("#") and cur is not None:
                cur["url"] = line
                chans.append(cur)
                cur = None
    return chans


def load_channels():
    """Channels from the channels/ folder (each file = own group in Kodi,
    e.g. channels/UK.m3u -> group 'UK'), plus the old channels.m3u (group 'Translated')."""
    chans = []
    d = CFG["channels_dir"]
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            fp = os.path.join(d, fn)
            if fn.lower().endswith((".m3u", ".m3u8")) and os.path.isfile(fp):
                group = clean_name(os.path.splitext(fn)[0].replace("_", " "))
                part = read_m3u(fp, group)
                log(f"  group '{group}': {len(part)} channels")
                chans += part
    f = CFG["channels_file"]
    if os.path.isfile(f):
        part = read_m3u(f, "Translated")
        if part:
            log(f"  group 'Translated' (channels.m3u): {len(part)} channels")
        chans += part
    if not chans and CFG["stream_url"]:
        chans.append({"name": "Channel", "url": CFG["stream_url"], "lang": None,
                      "group": "Translated", "logo": "", "ua": "", "referer": ""})
    used = {}
    for c in chans:
        base = slug(c["name"])
        used[base] = used.get(base, 0) + 1
        c["id"] = base if used[base] == 1 else f"{base}-{used[base]}"
    return chans


def http_args(url, reconnect=False, ch=None):
    if not url.startswith("http"):
        return []
    ch = ch or {}
    a = ["-user_agent", ch.get("ua") or CFG["user_agent"], "-rw_timeout", "15000000"]
    if ch.get("referer"):
        a += ["-referer", ch["referer"]]
    if reconnect:
        a += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
    return a


def pick_streams(url, ch=None):
    """As up to v0.39: (maps, resolution). See pick_streams_info for the format (v0.40)."""
    maps, res, _prof = pick_streams_info(url, ch)
    return maps, res


def pick_streams_info(url, ch=None):
    """Pick the best allowed video stream and matching audio from an HLS list.
    v0.40: also returns the format (codecs, size, frames/s, audio) of the choice.

    With MAX_HEIGHT_STRICT=1 a 4K or unknown stream is not opened silently.
    This prevents a wrongly labelled channel from making the ingest much
    heavier than the configured limit.
    """
    try:
        p = subprocess.run(["ffprobe", "-v", "error"] + http_args(url, ch=ch)
                           + ["-show_streams", "-show_programs", "-of", "json", url],
                           capture_output=True, text=True, timeout=45)
        info = json.loads(p.stdout or "{}")
    except Exception:  # noqa: BLE001
        info = {}

    streams = info.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video" and s.get("height")]
    if not vids:
        # v0.35: do NOT refuse an unknown resolution (v0.34 did with MAX_HEIGHT_STRICT=1).
        # That simply happens when you join in the middle of a long GOP or when ffprobe is
        # slow for a moment; ffmpeg can copy such a stream fine. Tested: v0.34 then stayed
        # on the waiting screen forever.
        return ["-map", "0:v:0", "-map", "0:a:0?"], "?", None

    max_h = CFG["max_height"]
    allowed = [s for s in vids if max_h <= 0 or int(s["height"]) <= max_h]
    if not allowed and CFG["max_height_strict"]:
        lowest = min(int(s["height"]) for s in vids)
        return None, f"no stream <= {max_h}p (lowest offered: {lowest}p)", None

    def bitrate(stream):
        try:
            return int((stream.get("tags") or {}).get("variant_bitrate", 0) or 0)
        except (TypeError, ValueError):
            return 0

    # With an allowed variant the highest resolution wins, and at equal height
    # the highest bitrate. Without an allowed variant (strict off) we take
    # the smallest offered resolution instead, not the heaviest.
    if allowed:
        best = max(allowed, key=lambda s: (int(s["height"]), bitrate(s)))
    else:
        smallest = min(int(s["height"]) for s in vids)
        best = max((s for s in vids if int(s["height"]) == smallest), key=bitrate)

    audio = None
    for prog in info.get("programs", []):
        idx = [st.get("index") for st in prog.get("streams", [])]
        if best.get("index") in idx:
            auds = [st for st in prog.get("streams", []) if st.get("codec_type") == "audio"]
            if auds:
                audio = auds[0]["index"]
            break
    amap = ["-map", f"0:{audio}"] if audio is not None else ["-map", "0:a:0?"]
    video_index = best.get("index", 0)
    width = int(best.get("width") or 0)
    height = int(best.get("height") or 0)
    if audio is not None:
        astream = next((st for st in streams if st.get("index") == audio), None)
    else:
        astream = next((st for st in streams if st.get("codec_type") == "audio"), None)
    prof = make_profile(best, astream)
    return ["-map", f"0:{video_index}"] + amap, f"{width}x{height}", prof


def stop_proc(proc):
    """Stop ffmpeg cleanly and wait (no zombie processes)."""
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---- v0.40: waiting screen in exactly the same format as the channel ---------------
# Kodi gets one continuous stream: first the waiting screen, then the channel. Kodi
# (and ffmpeg) keep the format of the START of the stream. When the waiting screen was
# H.264 + AAC and the channel e.g. MP2, AC3 or HEVC, Kodi got data with the wrong
# label (test: 2280 audio errors after zapping to a channel with MP2 audio;
# with hardware decoding Kodi can hang or crash on that).
# v0.40: first measure the channel format (or take it from memory) and make the
# waiting screen in exactly that format: video codec, picture size, frames per
# second, audio codec, sample rate and channels.
DEFAULT_PROFILE = {"v": "h264", "w": 1920, "h": 1080, "pix": "yuv420p", "fps": "25/1",
                   "a": "aac", "ar": 48000, "ac": 2}
# No B-frames: the 2 s waiting-screen pieces are put after each other; with
# B-frames the decode time ran over the seam and gave a broken frame (tested).
VIDEO_ENC = {
    "h264": ["-c:v", "libx264", "-preset", "veryfast", "-bf", "0"],
    "hevc": ["-c:v", "libx265", "-preset", "ultrafast",
             "-x265-params", "log-level=error:bframes=0"],
    "mpeg2video": ["-c:v", "mpeg2video", "-b:v", "2M", "-bf", "0"],
}
AUDIO_ENC = {           # codec -> (ffmpeg encoder, max. channels)
    "aac": ("aac", 8), "aac_latm": ("aac", 8), "mp2": ("mp2", 2), "mp3": ("mp2", 2),
    "mp1": ("mp2", 2), "ac3": ("ac3", 6), "eac3": ("eac3", 6),
}
# MPEG audio (layer 1, 2, 3) is one kind in MPEG-TS; the decoder detects the layer itself.
# ffprobe sometimes calls the same audio mp3 (short probe) and sometimes mp2 (tested).
AUDIO_FAMILY = {"mp1": "mpa", "mp2": "mpa", "mp3": "mpa"}


def audio_family(codec):
    return AUDIO_FAMILY.get(codec, codec)
LAYOUTS = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 8: "7.1"}


def fps_value(fps):
    try:
        a, b = str(fps).split("/") if "/" in str(fps) else (fps, 1)
        return float(a) / float(b) if float(b) else 0.0
    except (TypeError, ValueError):
        return 0.0


def clean_profile(p):
    """Format with safe values. A video or audio codec we cannot produce ourselves
    is kept (for comparison); the waiting screen then uses the default."""
    if not p:
        return None
    q = dict(DEFAULT_PROFILE)
    q.update({k: v for k, v in p.items() if v not in (None, "", 0)})
    try:
        q["w"], q["h"] = max(2, int(q["w"])) // 2 * 2, max(2, int(q["h"])) // 2 * 2
    except (TypeError, ValueError):
        q["w"], q["h"] = DEFAULT_PROFILE["w"], DEFAULT_PROFILE["h"]
    if not 8.0 <= fps_value(q["fps"]) <= 61.0:
        q["fps"] = "25/1"
    if q["pix"] not in ("yuv420p", "yuv420p10le"):
        q["pix"] = "yuv420p"
    try:
        q["ar"], q["ac"] = int(q["ar"]), max(1, int(q["ac"]))
    except (TypeError, ValueError):
        q["ar"], q["ac"] = 48000, 2
    if q["ar"] not in (32000, 44100, 48000):
        q["ar"] = 48000
    return q


def as_profile(size_or_prof):
    """'1280x720' (as up to v0.39) or a format dict -> format dict."""
    if isinstance(size_or_prof, dict):
        return clean_profile(size_or_prof)
    m = re.fullmatch(r"(\d+)x(\d+)", str(size_or_prof or ""))
    if m:
        return clean_profile(dict(DEFAULT_PROFILE, w=int(m.group(1)), h=int(m.group(2))))
    return clean_profile(DEFAULT_PROFILE)


def make_profile(v, a):
    """Format from ffprobe data of the chosen video and audio stream."""
    if not v:
        return None
    fps = v.get("avg_frame_rate")
    if not 8.0 <= fps_value(fps) <= 61.0:
        fps = v.get("r_frame_rate")
    p = {"v": v.get("codec_name"), "w": v.get("width"), "h": v.get("height"),
         "pix": v.get("pix_fmt"), "fps": fps}
    if a:
        p.update(a=a.get("codec_name"), ar=a.get("sample_rate"), ac=a.get("channels"))
    return clean_profile(p)


def profile_text(p):
    if not p:
        return "unknown"
    return (f"{p['v']} {p['w']}x{p['h']} {fps_value(p['fps']):.4g} bps, "
            f"audio {p['a']} {p['ar']} Hz {p['ac']} ch.")


def placeholder_key(p):
    raw = f"{p['v']}_{p['w']}x{p['h']}_{p['pix']}_{p['fps']}_{p['a']}_{p['ar']}_{p['ac']}"
    return re.sub(r"[^A-Za-z0-9_.x-]", "-", raw)


def audio_encode_args(p):
    """ffmpeg arguments to make audio in format p (or to convert to it)."""
    enc, max_ch = AUDIO_ENC.get(p["a"], AUDIO_ENC["aac"])
    ch = min(p["ac"], max_ch)
    rate = {"mp2": "192k", "ac3": "384k" if ch > 2 else "192k",
            "eac3": "384k" if ch > 2 else "192k"}.get(p["a"], "128k")
    return ["-c:a", enc, "-b:a", rate, "-ar", str(p["ar"]), "-ac", str(ch)]


def audio_mux_args(p):
    return ["-mpegts_flags", "latm"] if p and p.get("a") == "aac_latm" else []


def make_placeholder(path, line1, line2, size="1280x720"):
    """2 s screen with two lines of text (waiting / not reachable).
    v0.25: in the same format as the channel (otherwise green picture with VDPAU in Kodi).
    v0.40: also the same video and audio codec, frames per second and channels."""
    p = as_profile(size)
    w, h = p["w"], p["h"]
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    fs1 = max(18, min(64, w // 20))
    fs2 = max(14, fs1 * 40 // 64)
    vf = ["null"]
    if os.path.exists(font):
        if line2:
            vf = [f"drawtext=fontfile={font}:text='{line1}':fontsize={fs1}:"
                  f"fontcolor=white:x=(w-tw)/2:y=(h/2)-{fs1 + 6}",
                  f"drawtext=fontfile={font}:text='{line2}':fontsize={fs2}:"
                  f"fontcolor=0xBBBBBB:x=(w-tw)/2:y=(h/2)+{fs2 - 10}"]
        else:                                     # one line, in the middle
            vf = [f"drawtext=fontfile={font}:text='{line1}':fontsize={fs1}:"
                  f"fontcolor=white:x=(w-tw)/2:y=(h-th)/2"]
    gop = str(max(1, round(fps_value(p["fps"]) * 2)))
    ch = min(p["ac"], AUDIO_ENC.get(p["a"], AUDIO_ENC["aac"])[1])
    layout = LAYOUTS.get(ch, "stereo")

    def run(prof, venc):
        cmd = (["ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"color=c=0x101418:s={w}x{h}:r={prof['fps']}",
                "-f", "lavfi", "-i", f"anullsrc=r={prof['ar']}:cl={layout}",
                "-t", "2", "-vf", ",".join(vf + [f"format={prof['pix']}"])]
               + venc + ["-g", gop, "-pix_fmt", prof["pix"]]
               + audio_encode_args(prof) + audio_mux_args(prof) + ["-f", "mpegts", path])
        return subprocess.run(cmd, capture_output=True, text=True)

    r = run(p, VIDEO_ENC.get(p["v"], VIDEO_ENC["h264"]))
    if r.returncode != 0:
        # e.g. 10-bit HEVC or an unusual sample rate: then the default format (as v0.39)
        log(f"Waiting screen in format {profile_text(p)} failed "
            f"({r.stderr.strip()[-160:]}); default H.264/AAC")
        p = clean_profile(dict(DEFAULT_PROFILE, w=w, h=h))
        layout = "stereo"
        r = run(p, VIDEO_ENC["h264"])
        r.check_returncode()


# ---- v0.25: waiting screen in the same format as the channel ----------------------
# Kodi with VDPAU (hardware decoding) gave a green picture when the format changed
# from waiting screen (1280x720) to the channel (e.g. 1920x1080).
# v0.40: also remember the codecs per channel (channel-formats.json in the data folder).
_PH_LOCK = threading.Lock()
_RES_FILE = os.path.join(CFG["model_dir"], "channel-formats.json")
_RES = {}


def _load_res():
    global _RES
    try:
        with open(_RES_FILE) as f:
            _RES = json.load(f)
    except Exception:  # noqa: BLE001
        _RES = {}


def _write_res():
    try:
        tmp = _RES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_RES, f, indent=0, sort_keys=True)
        os.replace(tmp, _RES_FILE)
    except Exception as e:  # noqa: BLE001
        log("Channel format not saved:", e)


def _save_res(cid, size):
    """Only the picture size (as up to v0.39), when there is no complete format."""
    old = _RES.get(cid)
    if old == size or (isinstance(old, dict) and old.get("size") == size):
        return
    _RES[cid] = size
    _write_res()


def _save_profile(cid, prof):
    """v0.40: remember the complete format of the channel (fast zapping next time)."""
    if not prof:
        return
    entry = dict(prof, size=f"{prof['w']}x{prof['h']}")
    if _RES.get(cid) == entry:
        return
    _RES[cid] = entry
    _write_res()


def remembered_profile(ch):
    entry = _RES.get(ch["id"])
    if isinstance(entry, dict) and entry.get("v"):
        return clean_profile({k: v for k, v in entry.items() if k != "size"})
    return None


def guess_size(ch):
    """Format of the channel: measured before, else from the name ('(720p)'), else 1080p."""
    entry = _RES.get(ch["id"])
    if isinstance(entry, dict) and entry.get("size"):
        return entry["size"]
    if isinstance(entry, str):
        return entry
    m = re.search(r"\((\d{3,4})[pi]\)", ch["name"])
    h = int(m.group(1)) if m else 1080
    h = min(h, CFG["max_height"])
    w = {480: 854, 576: 1024, 720: 1280, 1080: 1920, 2160: 3840}.get(h, round(h * 16 / 9 / 2) * 2)
    return f"{w}x{h}"


def _ph_dir():
    """v0.40: keep waiting screens in the data folder (survives a restart), else in /work."""
    d = os.path.join(CFG["model_dir"], "placeholders")
    try:
        os.makedirs(d, exist_ok=True)
        if os.access(d, os.W_OK):
            return d
    except OSError:
        pass
    return CFG["work"]


def placeholder_for(kind, size):
    """Waiting screen ('waiting') or error screen ('offline') in this format; made once.
    size: '1280x720' or (v0.40) a format dict of the channel."""
    p = as_profile(size)
    mode = "dvb" if dvb_mode() else "ts"
    path = os.path.join(_ph_dir(), f"{VERSION}_{mode}_{kind}_{placeholder_key(p)}.ts")
    with _PH_LOCK:
        if not os.path.exists(path):
            tmp = path + ".making.ts"
            if kind == "offline":
                make_placeholder(tmp, "Channel not reachable", "please choose another channel", p)
            else:
                make_placeholder(tmp, "Translating - please wait", "", p)
            if dvb_mode():
                add_empty_dvb(tmp)
            os.replace(tmp, path)
    return path


def style_args():
    """One shared box behind all lines (BorderStyle 4), without overlap.
    SUB_BOX=0: no box, only an outline around the letters."""
    if CFG["box"] > 0:
        return {"bs": 4, "box": ass_alpha(CFG["box"]), "oc": ass_alpha(60),
                "outline": CFG["outline"], "shadow": CFG["shadow"]}
    return {"bs": 1, "box": ass_alpha(60), "oc": ass_alpha(90),
            "outline": max(3, CFG["outline"]), "shadow": CFG["shadow"]}


# ---------------------------------------------------------------- roll-up subtitles
def cue_text(c):
    """c = (start, reading time, text, original, original-to-show, group)."""
    text = ass_escape(c[2])
    orig = c[4] if len(c) > 4 else c[3]
    if CFG["show_original"] and orig and orig != c[2]:
        text += "\\N{\\rEN}" + ass_escape(orig)
    return text


def screen_lines(text):
    """v0.32: text split into lines as it appears on screen (same font, size
    and width as the DVB subtitle; 16:9)."""
    try:
        from dvbsub import split_lines
        return split_lines(text, 1280, 720, dvb_style())
    except Exception:  # noqa: BLE001  (e.g. no Pillow): rough estimate in characters
        per = max(20, int(1700 / max(20, CFG["font_size"])) * 2)
        words, lines, cur = text.split(), [], ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > per:
                lines.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        return lines + ([cur] if cur else [])


def chunk_lines():
    if CFG["chunk_lines"] > 0:
        return CFG["chunk_lines"]
    return max(1, CFG["sub_max_lines"] - 1) if CFG["lines"] >= 2 else max(1, CFG["sub_max_lines"])


def last_origs(history, n):
    """v0.32: original text of the last n sentences (parts of one sentence count as one)."""
    out, seen = [], set()
    for c in reversed(history):
        g = c[5] if len(c) > 5 else id(c)
        if g in seen:
            continue
        seen.add(g)
        out.append(c[3])
        if len(out) >= n:
            break
    return list(reversed(out))


class SubScheduler:
    """v0.36: spreads the lines so that they can be read.
    v0.35 showed the lines at the pace of the speech. With a simulated reader
    (12 characters/s, line by line) on a 15 min log: 25% of the lines could not
    be read completely before they left the screen.
    How it works: the planner reads along. With roll-up a new line pushes the oldest
    line off the screen; that new line only comes when the oldest line could have
    been read. A line also stays at least SUB_MIN_SECONDS as the newest line.
    With fast speech the subtitle therefore lags a bit; it catches up in pauses.
    Never more than SUB_MAX_DELAY seconds behind the speech."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.placed = deque(maxlen=8)   # (start, reader done) of the last lines
        self.lag = 0.0

    def place(self, cues):
        out = []
        n = visible_cues()
        cps = CFG["read_cps"]
        for c in sorted(cues, key=lambda x: x[0]):
            spoken = c[0]
            t = spoken
            if self.placed:
                prev_t, prev_done = self.placed[-1]
                t = max(t, prev_t + CFG["read_min"])            # no flashing
                if len(self.placed) >= n:
                    t = max(t, self.placed[-n][1])              # oldest line read?
            if t - spoken > CFG["read_max_delay"]:
                t = spoken + CFG["read_max_delay"]              # too far behind: catch up
            reader_start = max(t, self.placed[-1][1]) if self.placed else t
            done = reader_start + len(c[2]) / cps
            self.placed.append((t, done))
            self.lag = t - spoken
            out.append((t, max(done, t + CFG["read_min"])) + tuple(c[2:6]) + (spoken,))
        return out


def visible_cues():
    """How many subtitles (sentences or parts) on screen at once."""
    return max(1, CFG["lines"])


def segment_cues(items):
    """items: (spoken_start, spoken_end, translation, original, group) of one piece.
    Per sentence; a long sentence in parts of whole lines after each other (v0.32)."""
    out = []
    for a, end, dst, orig, group in items:
        out += make_cues(a, end, dst, orig, group)
    return out


def make_cues(a, speech_end, dst, orig, group):
    """v0.32: a long sentence does not fit in SUB_MAX_LINES lines. Before, the start was
    dropped (in a 7 min log: 9% of the words never on screen). Now: in parts of
    whole lines, shown after each other, spread over the time it is spoken."""
    lines = screen_lines(dst) or [dst]
    n = chunk_lines()
    chunks = [" ".join(lines[i:i + n]) for i in range(0, len(lines), n)]
    total = sum(len(c) for c in chunks) or 1
    span = max(0.0, speech_end - a)
    cues, done = [], 0
    for i, ch in enumerate(chunks):
        t = a + span * done / total
        if cues:
            t = max(t, cues[-1][0] + 0.8)      # not faster than 0.8 s per part
        done += len(ch)
        read = t + max(2.0 if len(chunks) > 1 else 2.5, 0.07 * len(ch))
        cues.append((t, read, ch, orig, orig if i == 0 else "", group))
    return cues


def old_tag():
    """ASS colour code for the older sentence(s). ASS writes colours as &HBBGGRR&."""
    r, g, b = _rgb(CFG["old_color"], (230, 210, 160))
    return "{\\c&H%02X%02X%02X&}" % (b, g, r)


def rollup_events(cues, w0, w1):
    """Like subtitles on TV: the last SUB_LINES sentences stay on screen.
    A new sentence comes at the bottom, the older one moves up (v0.26: beige, was grey).
    A sentence disappears only when there are SUB_LINES newer ones, or after SUB_HOLD
    seconds (but never before the minimum reading time)."""
    lines = visible_cues()
    cues = sorted(cues, key=lambda c: c[0])
    spans = []
    for i, c in enumerate(cues):
        a, read = c[0], c[1]
        b = a + max(CFG["hold"], read - a)
        if i + lines < len(cues):
            b = min(b, max(cues[i + lines][0], read))
        spans.append((a, b, c))
    marks = sorted({w0, w1} | {t for a, b, _ in spans for t in (a, b) if w0 < t < w1})
    events = []
    for t0, t1 in zip(marks, marks[1:]):
        vis = [c for a, b, c in spans if a <= t0 < b][-lines:]
        if not vis:
            continue
        parts = [(old_tag() + cue_text(c) + "{\\r}") for c in vis[:-1]]
        parts.append(cue_text(vis[-1]))
        text = "\\N".join(parts)
        if events and events[-1][2] == text and abs(events[-1][1] - t0) < 1e-6:
            events[-1] = (events[-1][0], t1, text)
        else:
            events.append((t0, t1, text))
    return events


# ---------------------------------------------------------------- playlist
def track_mode():
    return CFG["sub_mode"] == "track"


def dvb_mode():
    return CFG["sub_mode"] == "dvb"


def burn_mode():
    return CFG["sub_mode"] not in ("track", "dvb")


def _rgb(hexcol, default):
    try:
        h = hexcol.strip().lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:  # noqa: BLE001
        return default


# Older sentence beige, newest white; adjustable (SUB_OLD_COLOR / SUB_NEW_COLOR or /settings).
# Functions (no fixed values), so that a change on /settings works immediately.
def dvb_old_color():
    return _rgb(CFG["old_color"], (230, 210, 160))


def dvb_new_color():
    return _rgb(CFG["new_color"], (255, 255, 255))


DVB_YELLOW = (255, 229, 0)


def events_to_blocks(events):
    """Roll-up text (with ASS styling) -> lines with colour for the DVB pictures."""
    out = []
    for a, b, text in events:
        blocks = []
        old, new_col, tag = dvb_old_color(), dvb_new_color(), old_tag()
        for part in text.split("\\N"):
            col = new_col
            if part.startswith(tag):
                col = old
            if "{\\rEN}" in part:
                col = DVB_YELLOW
            plain = re.sub(r"\{[^}]*\}", "", part).strip()
            if plain:
                blocks.append((plain, col))
        if blocks:
            out.append((a, b, blocks))
    return out


def dvb_style():
    return {"font_size": CFG["font_size"], "outline": CFG["outline"], "box": CFG["box"],
            "font_file": CFG["font_file"],
            "margin": CFG["margin"], "position": CFG["position"],
            "max_lines": max(1, CFG["sub_max_lines"]),
            "max_chars": max(0, CFG["sub_max_chars"])}


def dvb_canvas(w, h):
    """Subtitle canvas: an HD picture gets 1280x720 (Kodi scales up), so that a
    picture always fits in one DVB packet (max. 64 KB)."""
    if not w or not h:
        return 1280, 720
    if h > 720:
        return 1280, 720
    return w - (w % 2), h - (h % 2)


def video_size(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        w, h = out.splitlines()[0].split(",")[:2]
        return int(w), int(h)
    except Exception:  # noqa: BLE001
        return 0, 0


def seg_format(path):
    """v0.40: codecs of a .ts file: {'v','w','h','a','ar','ac'} (empty on error)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "stream=codec_type,codec_name,width,height,sample_rate,channels",
                              "-of", "json", path], capture_output=True, text=True, timeout=20).stdout
        streams = json.loads(out or "{}").get("streams", [])
    except Exception:  # noqa: BLE001
        return {}
    v = next((x for x in streams if x.get("codec_type") == "video"), None)
    a = next((x for x in streams if x.get("codec_type") == "audio"), None)
    f = {}
    if v:
        f.update(v=v.get("codec_name"), w=int(v.get("width") or 0), h=int(v.get("height") or 0))
    if a:
        f.update(a=a.get("codec_name"), ar=int(a.get("sample_rate") or 48000),
                 ac=int(a.get("channels") or 2))
    return f


_PH_FMT = {}


def placeholder_format(path):
    """v0.40: the real format of the waiting screen (= what Kodi gets first)."""
    if path not in _PH_FMT:
        _PH_FMT[path] = seg_format(path)
    return _PH_FMT[path]


SUB_EDGE = 0.02


def edge_events(events, w0, w1):
    """v0.40: keep subtitles 0.02 s inside the edges of the piece.
    When a subtitle ran over the border of two pieces, 'clear' (end of
    piece 1) and 'show again' (start of piece 2) got exactly the same time. ffmpeg
    then glued them together and the subtitle broke ('Junk in packet'): it
    disappeared until the next sentence (test: 1 in ~16 borders)."""
    out = []
    for a, b, text in events:
        a2 = max(a, w0 + SUB_EDGE)
        b2 = min(b, w1 - SUB_EDGE)
        if b2 - a2 >= 0.04:
            out.append((a2, b2, text))
    return out


SUB_CUT_AFTER = 0.001       # v0.41: clear 1 ms after the last frame of the piece
SUB_CUT_GAP = 0.002         # v0.41: next piece shows the sentence again 2 ms later


def video_frames(path):
    """v0.41: display times of all frames in a .ts piece (sorted), or []."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return []
    vals = []
    for line in out.split():
        try:
            vals.append(float(line.split(",")[0]))
        except ValueError:
            pass
    return sorted(vals)


def video_pts_range(path):
    """v0.41: (first, last) display time of the frames, or (None, None)."""
    frames = video_frames(path)
    return (frames[0], frames[-1]) if frames else (None, None)


def snap_after_frame(t, frames):
    """v0.41: put a change just after the frame before it (+1 ms). On screen nothing
    changes (the first frame with the new text stays the same), but 'clear' and
    'show' (1/90000 s apart) never fall exactly on a frame any more. v0.40: when a
    change fell exactly on a frame, that frame had no subtitle."""
    if not frames:
        return t
    i = bisect.bisect_right(frames, t) - 1
    if i < 0:
        return t
    t2 = frames[i] + SUB_CUT_AFTER
    if i + 1 < len(frames) and t2 >= frames[i + 1]:
        return t
    return t2


def boundary_events(events, w0, w1, cut_in=None, cut_out=None, frames=None):
    """v0.41: subtitles of one piece, without a blink on the border.
    v0.40 removed a sentence that ran over the border of two pieces for 0.04 s (0.02 s
    before and after the border) and then showed it again. Those 0.04 s always
    contained a frame without subtitle: the sentence blinked.
    Now: clear 1 ms after the last frame of this piece (cut_out), and the next
    piece shows the sentence again 2 ms later (cut_in). No frame falls in between,
    and 'clear' and 'show' still have different times (otherwise ffmpeg glues them:
    'Junk in packet'). Without cut_in/cut_out: as v0.40 (0.02 s inside the edges).
    frames: display times of the frames; changes inside the piece come just after a
    frame (snap_after_frame)."""
    lo = w0 + SUB_EDGE if cut_in is None else cut_in
    hi = w1 - SUB_EDGE if cut_out is None else cut_out
    out = []
    for a, b, text in events:
        a2 = lo if a <= w0 + 1e-6 else max(snap_after_frame(a, frames), lo)
        b2 = hi if b >= w1 - 1e-6 else min(snap_after_frame(b, frames), hi)
        if b2 - a2 >= 0.04:
            out.append((a2, b2, text))
    return out


def mux_dvb(seg, sup, out_path, offset=0.0, audio_args=None, mux_args=(), silence=None):
    """Copy video+audio, add subtitle pictures as DVB subtitle.
    v0.31: offset = shift to our own continuous timeline for Kodi.
    v0.40: audio_args = convert audio (only when the channel sends a different audio
    format than Kodi already got); otherwise just copy.
    v0.40: silence = (start, duration, format): the audio of this piece is broken; silence
    in the format Kodi got, so the video keeps running (otherwise Kodi's buffer ran empty)."""
    extra_in, amap = [], ["-map", "0:a:0?"]
    if silence:
        t0, dur, fmt = silence
        ch = min(fmt.get("ac", 2), AUDIO_ENC.get(fmt.get("a"), AUDIO_ENC["aac"])[1])
        extra_in = ["-itsoffset", f"{t0:.6f}", "-f", "lavfi", "-t", f"{max(0.1, dur):.3f}",
                    "-i", f"anullsrc=r={fmt.get('ar', 48000)}:cl={LAYOUTS.get(ch, 'stereo')}"]
        amap = ["-map", "2:a:0"]
        audio_args = audio_encode_args({"a": fmt.get("a", "aac"), "ar": fmt.get("ar", 48000),
                                        "ac": fmt.get("ac", 2)})
        mux_args = audio_mux_args(fmt)
    cmd = (["ffmpeg", "-v", "error", "-nostdin", "-y", "-copyts", "-i", seg,
            "-fix_sub_duration", "-i", sup] + extra_in +
           ["-map", "0:v:0"] + amap + ["-map", "1:0",
            "-c:v", "copy"] + (audio_args or ["-c:a", "copy"]) + ["-c:s", "dvb_subtitle",
            "-metadata:s:s:0", f"language={sub_lang_code()}",
            "-output_ts_offset", f"{offset:.6f}",
            "-muxdelay", "0", "-muxpreload", "0"] + list(mux_args) + ["-f", "mpegts", out_path])
    return subprocess.run(cmd, capture_output=True, text=True)


def add_empty_dvb(path):
    """Also give the waiting screen an (empty) subtitle stream, so that every piece
    has the same tracks."""
    from dvbsub import write_sup
    w, h = video_size(path)
    W, H = dvb_canvas(w, h)
    start, _ = probe(path)
    sup = path + ".sup"
    write_sup(sup, [], W, H, dvb_style(), t_clear_at=start)
    tmp = path + ".dvb.ts"
    p = mux_dvb(path, sup, tmp)
    if p.returncode == 0:
        os.replace(tmp, path)
    else:
        log("Empty subtitle stream for waiting screen failed:", p.stderr[-300:])
    for f in (sup, tmp):
        try:
            os.remove(f)
        except OSError:
            pass


def out_window():
    """v0.15: with track keep at least 20 pieces (160 s) in the list. With 10,
    inputstream.adaptive could look for a piece after a stall that was already gone
    ('Segment at position 0 not found')."""
    return max(CFG["window"], 20) if track_mode() else CFG["window"]


def vtt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"


def ass_to_plain(text):
    """Roll-up text (ASS styling) -> plain text for WebVTT."""
    text = re.sub(r"\{[^}]*\}", "", text).replace("\\N", "\n")
    return text.replace("-->", "->").strip()


def write_vtt(path, events, start):
    """WebVTT for one piece. Times are the real stream times (PTS);
    X-TIMESTAMP-MAP links them to the video, so that both players show them correctly."""
    lines = ["WEBVTT",
             f"X-TIMESTAMP-MAP=MPEGTS:{int(round(start * 90000)) % (1 << 33)},LOCAL:{vtt_time(start)}",
             ""]
    for a, b, text in events:
        plain = ass_to_plain(text)
        if plain and b > a:
            lines += [f"{vtt_time(a)} --> {vtt_time(b)} line:90% align:center", plain, ""]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


EMPTY_VTT = "WEBVTT\n\n"


class Playlist:
    """Live playlist. With SUB_MODE=track three files:
    index.m3u8 (master) -> video.m3u8 (video+audio) + subs.m3u8 (subtitles)."""

    def __init__(self, out_dir):
        self.out = out_dir
        self.entries = deque()   # (seq, ts name, duration, disc)
        self.trash = deque()
        self.seq = 0
        self.disc_seq = 0
        self.real = False
        # v0.15 (bug in v0.14): TARGETDURATION was 16 with pieces of 8 s.
        # inputstream.adaptive refreshes the list at that rhythm, so saw new pieces
        # too late, ran empty ('stream stalled') and then hung when zapping.
        self.target = CFG["seg_seconds"] + 1
        self.lock = threading.Lock()

    def write_master(self):
        name = CFG["sub_lang_name"].replace('"', "")
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",'
                 f'LANGUAGE="{CFG["dst_lang"]}",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,URI="subs.m3u8"',
                 '#EXT-X-STREAM-INF:BANDWIDTH=8000000,SUBTITLES="subs"',
                 "video.m3u8"]
        self._save("index.m3u8", lines)

    def add_batch(self, items):
        """v0.16: several waiting-screen pieces at once, with one playlist write."""
        with self.lock:
            for name, dur in items:
                self.entries.append((self.seq, name, dur, False))
                self.seq += 1
            self.write()

    def add(self, name, dur, disc, placeholder=False):
        with self.lock:
            if placeholder and self.real:
                return False
            if not placeholder:
                self.real = True
            self.entries.append((self.seq, name, dur, disc and len(self.entries) > 0))
            self.seq += 1
            while len(self.entries) > out_window():
                old = self.entries.popleft()
                if old[3]:
                    self.disc_seq += 1
                self.trash.append(old[1])
            while len(self.trash) > 6:   # players may still read old pieces for a while
                old = self.trash.popleft()
                for fn in (old, vtt_name(old)):
                    try:
                        os.remove(os.path.join(self.out, fn))
                    except OSError:
                        pass
            self.target = max(self.target, math.ceil(dur))
            self.write()
            return True

    def next_name(self, prefix):
        with self.lock:
            return f"{prefix}{self.seq:07d}.ts"

    def write(self):
        if len(self.entries) < 3:
            return
        head = ["#EXTM3U", "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{self.target}",
                f"#EXT-X-MEDIA-SEQUENCE:{self.entries[0][0]}",
                f"#EXT-X-DISCONTINUITY-SEQUENCE:{self.disc_seq}"]
        video, subs = list(head), list(head)
        for seq, name, dur, disc in self.entries:
            for lst, fn in ((video, name), (subs, vtt_name(name))):
                if disc:
                    lst.append("#EXT-X-DISCONTINUITY")
                lst.append(f"#EXTINF:{dur:.3f},")
                lst.append(fn)
        if track_mode():
            # subtitles first, then video: the player never finds video without subtitles
            self._save("subs.m3u8", subs)
            self._save("video.m3u8", video)
        else:
            self._save("index.m3u8", video)

    def _save(self, fname, lines):
        tmp = os.path.join(self.out, fname + ".tmp")
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, os.path.join(self.out, fname))


def vtt_name(ts_name):
    return ts_name[:-3] + ".vtt"


def video_args():
    enc = CFG["encoder"]
    br = CFG["bitrate"]
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-b:v", br,
                "-maxrate", br, "-bufsize", br, "-profile:v", "high",
                "-forced-idr", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", br,
            "-maxrate", br, "-bufsize", br]


# ---------------------------------------------------------------- one channel
class Pipeline:
    def __init__(self, ch, tr, lt):
        self.ch, self.tr, self.lt = ch, tr, lt
        self.ev = threading.Event()          # stop signal for this channel
        self.out = os.path.join(CFG["out"], ch["id"])
        self.buf = os.path.join(CFG["work"], ch["id"], "buf")
        self.subs = os.path.join(CFG["work"], ch["id"], "subs")
        self.pl = Playlist(self.out)
        self.q_in, self.q_enc = queue.Queue(), queue.Queue()
        self.proc = None
        self.failed = 0
        self.had_speech = False
        self.n_out = 0
        self.threads = []
        self.status = {"channel": ch["name"], "state": "starting",
                       "started": time.strftime("%H:%M:%S")}
        self.size = guess_size(ch)           # v0.25: format of the waiting screen
        self.status["waiting_screen"] = self.size
        # v0.40: format (codecs) of the channel: measured when opening, or remembered.
        # The waiting screen gets exactly this format, so Kodi sees no change.
        self.mem_prof = remembered_profile(ch)
        self.src_prof = None                 # measured when opening the channel
        self.probe_done = threading.Event()
        self.kodi_prof = None                # chosen format for the waiting screen
        self.kodi_fmt = None                 # what Kodi really got first (measured)
        self.fmt_logged = set()
        self.last_time_fix_log = 0.0
        self.sub_cut = None              # v0.41: (border, clear time, shift) of previous piece
        # v0.31: own continuous timeline to Kodi (SUB_MODE=dvb only).
        # clock = end of the last passed piece on that timeline.
        self.clock_lock = threading.Lock()
        self.clock = 0.0
        self.offset = None
        self.t_first = None                  # when the first piece was ready
        self.force_disc = False
        self.ph_info = {}                    # waiting-screen file -> (start, duration)
        self.lead_min = None                 # smallest buffer since the previous overview
        self.last_low_log = 0.0
        self.ai_times = []
        self.asr_prev = dict(tr.stats) if getattr(tr, "stats", None) else None
        self.n_real = 0
        self.cue_group = 0                   # v0.32: parts of the same sentence
        self.sched = SubScheduler()          # v0.36: readable pace
        self.lag_times = []
        for k in ("source_interruptions", "source_time_jumps", "buffer_almost_empty"):
            self.status[k] = 0

    def start(self):
        for d in (self.out, self.buf, self.subs):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
        if track_mode():
            self.pl.write_master()
        channel_lang = self.ch["lang"] if CFG["use_playlist_lang"] else None
        self.tr.set_channel(channel_lang)
        for fn in (self.filler_loop, self.ingest_loop, self.scan_loop,
                   self.transcribe_loop, self.encode_loop, self.monitor_loop):
            t = threading.Thread(target=self.safe(fn), daemon=True)
            t.start()
            self.threads.append(t)

    def safe(self, fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                # v0.34: do not let a pipeline keep running half-active after an
                # essential worker stopped unexpectedly. The next Kodi request
                # can start the same channel again via Manager.touch().
                if fn.__name__ in ("monitor_loop", "filler_loop"):
                    # v0.35: overview and waiting screen are not needed for video and subtitles;
                    # an error there must not stop the channel (it did in v0.34).
                    log(f"[{self.ch['name']}] error in {fn.__name__} (channel keeps running): {e}")
                    return
                log(f"[{self.ch['name']}] FATAL worker error in {fn.__name__}: {e}")
                self.status["state"] = f"error: {fn.__name__}: {e}"
                self.ev.set()
                stop_proc(self.proc)
        return run

    def stop(self):
        self.ev.set()
        stop_proc(self.proc)
        for t in self.threads:
            t.join(timeout=15)
        # v0.13 (bug in v0.12): also remove the translated pieces in /out. Otherwise
        # every zap left ~100 MB behind in RAM (1 GB), until
        # burning in failed because /out was full.
        for d in (os.path.dirname(self.buf), self.out):
            shutil.rmtree(d, ignore_errors=True)

    # -- waiting screen until the first translated pieces are ready
    def choose_kodi_profile(self):
        """v0.40: choose the format for Kodi before the first waiting screen.
        Measured format > remembered format > default (H.264/AAC, estimated size)."""
        wait = min(3.0, CFG["profile_wait"]) if self.mem_prof else CFG["profile_wait"]
        t_end = time.time() + wait
        while not self.ev.is_set() and not self.probe_done.is_set() and time.time() < t_end:
            self.probe_done.wait(0.2)
        if self.src_prof:
            prof, how = self.src_prof, "measured"
        elif self.mem_prof:
            prof, how = self.mem_prof, "remembered"
        else:
            prof, how = as_profile(self.size), "unknown, default"
        if burn_mode():                      # burning in always makes H.264
            prof = clean_profile(dict(prof, v="h264", pix="yuv420p"))
        self.kodi_prof = prof
        self.size = f"{prof['w']}x{prof['h']}"
        self.status["waiting_screen"] = self.size
        self.status["format"] = profile_text(prof)
        log(f"[{self.ch['name']}] format ({how}): {profile_text(prof)} "
            "- waiting screen in the same format")
        if prof["v"] not in VIDEO_ENC or prof["a"] not in AUDIO_ENC:
            log(f"[{self.ch['name']}] NOTE: this format cannot be reproduced exactly; "
                "the waiting screen will be H.264/AAC (channel audio is converted)")

    def filler_loop(self):
        n = 0
        self.choose_kodi_profile()           # v0.40
        if track_mode():
            # v0.16 (bug in v0.15): Kodi starts LIVE_DELAY seconds behind live. When
            # opening there were only ~3 waiting-screen pieces of 2 s in the list, so
            # 24 s back did not exist ('Segment at position 0 not found').
            # Now put enough waiting screen ready at once.
            count = min(out_window(), math.ceil((CFG["live_delay"] + 10) / 2.0))
            batch = []
            for _ in range(count):
                name = f"w{n:07d}.ts"
                self.make_filler(name, n)
                batch.append((name, 2.0))
                n += 1
            self.pl.add_batch(batch)
        if dvb_mode():
            # v0.31: waiting screen on the same continuous timeline as the channel
            while not self.ev.is_set() and not self.pl.real:
                if not self.add_filler_dvb(f"w{n:07d}.ts"):
                    break
                n += 1
                if n >= 3:
                    self.ev.wait(2.0)
            return
        while not self.ev.is_set() and not self.pl.real:
            name = f"w{n:07d}.ts"
            self.make_filler(name, n)
            if not self.pl.add(name, 2.0, False, placeholder=True):
                break
            n += 1
            if n >= 3:
                self.ev.wait(2.0)

    def add_filler_dvb(self, name):
        """v0.31: one waiting-screen piece (2 s) after the previous one on the timeline."""
        src = placeholder_for("offline" if self.failed >= 3 else "waiting", self.kodi_prof or self.size)
        if self.kodi_fmt is None:
            self.kodi_fmt = placeholder_format(src)      # v0.40
        if src not in self.ph_info:
            self.ph_info[src] = probe(src)
        ps, pd = self.ph_info[src]
        pd = pd if 1.9 < pd < 2.3 else 2.0
        with self.clock_lock:
            if self.pl.real or self.ev.is_set():
                return False
            out = os.path.join(self.out, name)
            p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-copyts", "-i", src,
                                "-map", "0", "-c", "copy",
                                "-output_ts_offset", f"{self.clock - ps:.6f}",
                                "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", out],
                               capture_output=True, text=True)
            if p.returncode != 0:
                log("Waiting-screen piece failed:", p.stderr[-200:].strip())
                return True
            if not self.pl.add(name, pd, False, placeholder=True):
                return False
            if self.t_first is None:
                self.t_first = time.time()
            self.clock += pd
        return True

    def lead(self):
        """v0.31: estimate how many seconds of video are ready ahead of what Kodi shows now
        (assuming Kodi has played without pause since the first piece)."""
        if self.t_first is None:
            return None
        return self.clock - (time.time() - self.t_first)

    def make_filler(self, name, n):
        src = placeholder_for("offline" if self.failed >= 3 else "waiting", self.kodi_prof or self.size)
        if self.kodi_fmt is None:
            self.kodi_fmt = placeholder_format(src)      # v0.40
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0", "-c", "copy",
                        "-output_ts_offset", f"{n * 2.0:.1f}", "-f", "mpegts",
                        os.path.join(self.out, name)], capture_output=True)
        if track_mode():
            with open(os.path.join(self.out, vtt_name(name)), "w") as f:
                f.write(EMPTY_VTT)

    # -- 1. ingest
    def ingest_loop(self):
        url = self.ch["url"]
        last_select_warning = 0.0
        while not self.ev.is_set():
            maps, res, prof = pick_streams_info(url, self.ch)
            if prof and self.src_prof is None:
                self.src_prof = prof             # v0.40: for the waiting screen
            self.probe_done.set()
            if not maps:
                self.status["state"] = "no allowed stream (resolution too high)"
                self.status["resolution_choice"] = res
                self.failed = max(self.failed, 3)   # v0.35: screen 'Channel not reachable'
                                                    # instead of 'starting' forever
                if time.time() - last_select_warning >= 30:
                    log(f"[{self.ch['name']}] stream not opened: {res}; "
                        f"check MAX_HEIGHT={CFG['max_height']} and MAX_HEIGHT_STRICT")
                    last_select_warning = time.time()
                self.ev.wait(10)
                continue
            self.status["resolution_choice"] = res
            run_id = str(int(time.time()))
            cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
                   + http_args(url, reconnect=True, ch=self.ch) + ["-i", url] + maps
                   + ["-c", "copy", "-f", "hls", "-hls_time", str(CFG["seg_seconds"]),
                      "-hls_list_size", str(CFG["ingest_list_size"]),
                      "-hls_delete_threshold", str(CFG["ingest_delete_threshold"]),
                      "-hls_flags", "delete_segments+omit_endlist",
                      "-hls_segment_filename", os.path.join(self.buf, f"r{run_id}_%06d.ts"),
                      os.path.join(self.buf, "buf.m3u8")])
            log(f"[{self.ch['name']}] opening stream ({res}): {url}")
            if prof:
                _save_profile(self.ch["id"], prof)   # v0.40: also remember the codecs
            elif re.fullmatch(r"\d+x\d+", res or ""):
                _save_res(self.ch["id"], res)
            self.status["state"] = "opening stream"
            t_start = time.time()
            self.proc = subprocess.Popen(cmd)
            while self.proc.poll() is None and not self.ev.is_set():
                time.sleep(0.5)
            stop_proc(self.proc)
            if not self.ev.is_set():
                self.status["source_interruptions"] += 1      # v0.31
                self.failed = self.failed + 1 if time.time() - t_start < 30 else 1
                wait = 5 if self.failed < 3 else 20
                log(f"[{self.ch['name']}] stream stopped (code {self.proc.returncode}, "
                    f"attempt {self.failed}) - retrying in {wait} s")
                self.status["state"] = ("channel not reachable" if self.failed >= 3
                                           else "stream stopped, retrying")
                self.ev.wait(wait)

    def scan_loop(self):
        seen = set()
        playlist = os.path.join(self.buf, "buf.m3u8")
        while not self.ev.is_set():
            try:
                with open(playlist) as f:
                    names = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            except FileNotFoundError:
                names = []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    self.q_in.put(os.path.join(self.buf, os.path.basename(n)))
            if len(seen) > 1000:
                seen = set(names)
            self.ev.wait(0.5)

    # -- 2. recognise + translate
    def transcribe_loop(self):
        import numpy as np
        history = []                 # recent subtitles: (start, reading time, translation, original)
        tail = None                  # last seconds of audio of the previous piece
        held_pad = None              # v0.33: tail is a carried-over half sentence (piece before)
        prev_run, prev_end = None, None
        last_warn = last_skip_log = 0.0
        while not self.ev.is_set():
            try:
                seg = self.q_in.get(timeout=1)
            except queue.Empty:
                continue
            queued = False
            pre_run, pre_end = prev_run, prev_end      # v0.35: for error handling
            try:
                backlog = self.q_in.qsize()
                self.status["queue_segments"] = backlog
                self.status["queue_seconds_estimate"] = round(backlog * CFG["seg_seconds"], 1)
                if backlog >= CFG["queue_warn_segments"] and time.time() - last_warn >= 30:
                    log(f"WARNING: audio queue {backlog} segments "
                        f"(~{backlog * CFG['seg_seconds']} s): the speech recognition is too slow")
                    last_warn = time.time()
                if not os.path.exists(seg):
                    self.status["missing_segments"] = self.status.get("missing_segments", 0) + 1
                    log(f"WARNING: HLS segment no longer exists before it could be processed: {os.path.basename(seg)}")
                    continue

                t0 = time.time()
                try:
                    made = os.path.getmtime(seg)
                except OSError:
                    made = t0
                start, dur, a_start, fixed = probe_sane(seg)      # v0.40
                run = os.path.basename(seg).split("_")[0]
                disc = prev_run is not None and (run != prev_run or prev_end is None
                                                 or abs(start - prev_end) > 2.0 or fixed)
                if fixed and time.time() - self.last_time_fix_log > 60:
                    self.last_time_fix_log = time.time()
                    log(f"[{self.ch['name']}] times in the source are wrong "
                        f"({os.path.basename(seg)}); piece passed on with duration {dur:.1f} s")
                if disc:
                    history = []
                    tail = None
                    self.tr.reset()
                    self.sched.reset()
                    if run == prev_run and prev_end is not None:   # v0.35: not after a skipped piece
                        self.status["source_time_jumps"] += 1
                        log(f"[{self.ch['name']}] time jump in the source "
                            f"({start - (prev_end or start):+.1f} s): timeline for Kodi just continues")
                # v0.31: the subtitle window of this piece starts where the previous one ended.
                # The audio of a piece often starts slightly before the end of the previous one;
                # without this, subtitles reached Kodi in the wrong order.
                if not disc and prev_end is not None and abs(start - prev_end) <= 2.0:
                    w0 = prev_end
                else:
                    w0 = start
                w1 = max(start + dur, w0 + 0.1)
                prev_run, prev_end = run, w1

                cues = []
                skip = CFG["max_backlog"] > 0 and backlog >= CFG["max_backlog"]
                if skip:
                    # v0.13 (optional): video continues, only this piece gets no new
                    # subtitles. This way a too large backlog catches up again.
                    tail = None
                    self.status["pieces_without_subtitles"] = self.status.get("pieces_without_subtitles", 0) + 1
                    if time.time() - last_skip_log > 30:
                        log(f"Backlog {backlog} pieces >= MAX_BACKLOG: temporarily pieces without new subtitles")
                        last_skip_log = time.time()
                audio = None if skip else read_audio(seg)
                if audio is not None and len(audio) > 1600:
                    # overlap: put the last seconds of the previous piece in front,
                    # so that sentences on the border are not lost
                    off = 0.0
                    full = audio
                    cut = None
                    if tail is not None:
                        full = np.concatenate([tail, audio])
                        off = len(tail) / 16000.0
                        # v0.33: carried-over half sentence: only skip the short bit before it,
                        # not the whole overlap (otherwise that sentence could be lost).
                        cut = held_pad if held_pad is not None else off
                    held_pad = None
                    n_tail = int(CFG["overlap"] * 16000)
                    tail = audio[-n_tail:] if n_tail > 0 else None
                    parts, src = self.speech_to_text(full)
                    # v0.33: half sentence at the end? Do not show it yet; the audio from the start
                    # of that sentence goes along to the next piece and is translated there in full.
                    parts, hold_at, held = split_unfinished(parts, len(full) / 16000.0, off)
                    if hold_at is not None:
                        i0 = max(0, int((hold_at - 0.4) * 16000))
                        tail = full[i0:]
                        held_pad = 0.4 if hold_at >= 0.4 else hold_at
                        self.status["half_sentences_carried"] = self.status.get("half_sentences_carried", 0) + 1
                        log(f"  [{max(0.0, hold_at - off):4.1f}s] (continues, follows in the next piece: "
                            f"{held[:50]}{'…' if len(held) > 50 else ''})")
                    self.status["language"] = src
                    self.status["language_fixed"] = self.tr.known_lang()
                    # Sentences completely inside the old overlap: skip.
                    # Sentences that start in the overlap and continue: cut off the old part.
                    clipped = []
                    for s, e, text in parts:
                        if e <= (off if cut is None else cut) + 0.02:
                            continue
                        if (s < off and history and similar(history[-1][3], text)
                                and same_start(history[-1][3], text)):
                            # v0.13: last time this sentence was cut off halfway;
                            # now it is complete. Remove the half version.
                            # v0.22: only when both sentences start the same. Otherwise
                            # e.g. 'The pipeline is fed out...' was lost for 'is fed out...'.
                            last_group = history[-1][5] if len(history[-1]) > 5 else None
                            history.pop()
                            while (history and last_group is not None and len(history[-1]) > 5
                                   and history[-1][5] == last_group):
                                history.pop()       # v0.32: all parts of that sentence
                            # v0.21: then still compare with the sentence before
                            # ('...extreme conversion.' + 'conversion. The train deck')
                        if s < off and history and CFG["overlap_trim"]:
                            # v0.20: sentence starts in the overlap and was (partly) shown already.
                            # Remove the repeated part, so nothing appears twice.
                            prev = " ".join(last_origs(history, 2))
                            if contained(text, history[-1][3]):
                                self.status["overlap_duplicates_removed"] = self.status.get("overlap_duplicates_removed", 0) + 1
                                continue
                            rest, k = trim_overlap(prev, text)
                            if k:
                                self.status["overlap_duplicates_removed"] = self.status.get("overlap_duplicates_removed", 0) + 1
                                if len(_WORD.findall(rest)) < 1:
                                    continue
                                text = rest
                        rel_start = max(0.0, s - off)
                        rel_end = max(rel_start + 0.05, e - off)
                        clipped.append((rel_start, rel_end, text))
                    parts = merge_parts(clipped)
                    parts = [(s, e, (t[:1] + t[1:].lower()) if is_shouting(t) else t)
                             for s, e, t in parts]            # v0.33
                    if parts:
                        nl = self.lt.translate([p[2] for p in parts], src)
                        items = []
                        for (s, e, orig), dst in zip(parts, nl):
                            dst = fix_case(orig, clean_dst(orig, dst))
                            a = max(w0, a_start + s)       # v0.31: count from the first audio
                            last = items[-1] if items else (history[-1] if history else None)
                            if last and last[2] == dst and a - last[0] < 3.0:
                                continue                   # v0.32: same sentence right after each other
                            self.cue_group += 1
                            items.append((a, a_start + e, dst, orig, self.cue_group))
                        cues = segment_cues(items)
                elif not skip:
                    tail = None
                self.had_speech = bool(cues)
                t_ai = time.time() - t0

                if cues:
                    cues = self.sched.place(cues)        # v0.36: readable pace
                    self.status["subtitle_behind_s"] = round(self.sched.lag, 1)
                    self.lag_times.append(self.sched.lag)
                history = [c for c in history if c[0] > start - 60] + cues
                events = rollup_events(history, w0, w1)
                self.ai_times.append(t_ai)

                ass_path = None
                if burn_mode():
                    ass_path = os.path.join(self.subs, os.path.basename(seg) + ".ass")
                    small = max(20, int(CFG["font_size"] * 0.7))
                    with open(ass_path, "w", encoding="utf-8") as f:
                        f.write(ASS_HEADER.format(font=CFG["font"], size=CFG["font_size"], small=small,
                                                 align=8 if CFG["position"] == "top" else 2,
                                                 margin=CFG["margin"],
                                                 **style_args()))
                        for a, b, text in events:
                            f.write(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},NL,,0,0,0,,{text}\n")
                for c in cues:
                    if c[4]:                               # first part of a sentence: log whole sentence
                        n_parts = sum(1 for x in cues if x[5] == c[5])
                        full = " ".join(x[2] for x in cues if x[5] == c[5])
                        log(f"  [{c[0] - start:4.1f}s] {c[4]}  ->  {full}"
                            + (f"   ({n_parts} parts)" if n_parts > 1 else ""))
                        add_recent(self.ch["name"], c[4], full, self.status.get("language"))
                log(f"[{self.ch['name']}] {os.path.basename(seg)}: {dur:.1f}s, AI {t_ai:.1f}s"
                    f"{' (skipped)' if skip else ''}, {len(cues)} sentences")
                self.status["queue_segments"] = self.q_in.qsize()
                self.status["queue_seconds_estimate"] = round(self.q_in.qsize() * CFG["seg_seconds"], 1)
                self.status["ai_s"] = round(t_ai, 2)
                self.q_enc.put((seg, ass_path, dur, disc, made, events, start, w0, w1))
                queued = True
            except Exception as e:  # noqa: BLE001
                # v0.35: one broken piece (ffprobe/ffmpeg stuck, odd audio, GPU error)
                # must not stop the channel. v0.33 then froze the picture, v0.34 stopped the
                # whole channel. Now: pass this piece on without new subtitles.
                self.status["pieces_error"] = self.status.get("pieces_error", 0) + 1
                if queued:
                    log(f"[{self.ch['name']}] error after passing on {os.path.basename(seg)}: {e}")
                    continue
                log(f"[{self.ch['name']}] ERROR with {os.path.basename(seg)}: {e} "
                    "- piece continues without new subtitles")
                tail, held_pad = None, None
                try:
                    start, dur, _a, _fixed = probe_sane(seg)      # v0.40
                    run = os.path.basename(seg).split("_")[0]
                    disc = pre_run is not None and (run != pre_run or pre_end is None
                                                    or abs(start - pre_end) > 2.0)
                    w0 = pre_end if (not disc and pre_end is not None) else start
                    w1 = max(start + dur, w0 + 0.1)
                    prev_run, prev_end = run, w1
                    events = rollup_events(history, w0, w1)   # text already shown stays
                    self.q_enc.put((seg, None, dur, disc, time.time(), events, start, w0, w1))
                except Exception as e2:  # noqa: BLE001
                    prev_end = None          # next piece reconnects to the timeline
                    log(f"[{self.ch['name']}] {os.path.basename(seg)} skipped: {e2}")

    def speech_to_text(self, audio):
        """Second attempt without voice filter only when someone was just speaking."""
        import numpy as np
        t0 = time.time()
        parts, src = self._speech_to_text(audio, vad=True)
        slow = time.time() - t0 > CFG["seg_seconds"] * 0.3     # no time for a second try
        if (not parts and not slow and self.had_speech and len(audio)
                and not self.tr.force_recheck
                and float(np.sqrt(np.mean(audio ** 2))) > 0.02):
            parts, src = self._speech_to_text(audio, vad=False)
            if parts:
                self.status["vad_retries"] = self.status.get("vad_retries", 0) + 1
        return parts, src

    def _speech_to_text(self, audio, vad=True):
        """Speech -> text in a language the translator can translate.
        If the translator does not know the language, the speech recognition first translates to English."""
        known = self.tr.known_lang()
        if known and known != "en" and known != CFG["dst_lang"] and (
                CFG["via_en"] == "always" or known in CFG["via_en_langs"] or
                (CFG["via_en"] == "auto" and not self.lt.supports(known))):
            if self.status.get("via") != "asr->en":
                log(f"[{self.ch['name']}] language '{known}': first to English")
            self.status["via"] = "asr->en"
            return self.tr.transcribe(audio, task="translate", force_lang=known, vad=vad)
        parts, src = self.tr.transcribe(audio, vad=vad)
        if (parts and src not in ("en", CFG["dst_lang"], "und") and
                CFG["via_en"] != "never" and
                (CFG["via_en"] == "always" or src in CFG["via_en_langs"] or
                 not self.lt.supports(src))):
            # language not fixed yet, but the translator does not know it: again, to English
            parts, src = self.tr.transcribe(audio, task="translate", force_lang=src, vad=vad)
            self.status["via"] = "asr->en"
            return parts, src
        self.status["via"] = self.lt.name if src != CFG["dst_lang"] else "none (already target language)"
        return parts, src

    # -- 3. output
    def encode_loop(self):
        first = True
        held = []      # v0.15: hold the first translated pieces for a moment (start buffer)
        pend = []      # v0.31 (dvb): same, waiting screen stays until BUFFER_SEGMENTS are ready
        while not self.ev.is_set():
            try:
                seg, ass_path, dur, disc, made, events, start, w0, w1 = self.q_enc.get(timeout=1)
            except queue.Empty:
                continue
            t0 = time.time()
            if dvb_mode():
                pend.append((seg, events, start, dur, disc or first, w0, made, w1))
                first = False
                if not self.pl.real and len(pend) < CFG["buffer_segments"]:
                    self.status["state"] = f"filling buffer ({len(pend)}/{CFG['buffer_segments']})"
                    continue
                if self.pl.real:
                    self.check_lead()          # just before a new piece: lowest point
                for p_seg, p_events, p_start, p_dur, p_disc, p_w0, p_made, p_w1 in pend:
                    name = f"s{self.n_out:07d}.ts"
                    self.n_out += 1
                    try:
                        ok = self.publish_dvb(p_seg, name, p_events, p_start, p_dur,
                                              p_disc or self.force_disc, p_w0, p_w1)
                    except Exception as e:  # noqa: BLE001
                        # v0.35: one piece that cannot be passed on no longer stops the channel
                        log(f"[{self.ch['name']}] piece {os.path.basename(p_seg)} not passed on: {e}")
                        self.status["pieces_error"] = self.status.get("pieces_error", 0) + 1
                        ok = False
                    if not ok:
                        self.force_disc = True     # next piece: reconnect the timeline
                        continue
                    self.force_disc = False
                    self.status.update({"state": "running", "last_piece": name,
                                        "pass_on_s": round(time.time() - t0, 2),
                                        "processing_behind_s": round(time.time() - p_made, 1),
                                        "time": time.strftime("%H:%M:%S")})
                pend = []
                if self.lead() is not None:
                    self.status["buffer_s"] = round(self.lead(), 1)
                continue
            name = f"s{self.n_out:07d}.ts"      # own counter: held pieces are unique too
            self.n_out += 1
            if track_mode():
                # v0.14: pass video and audio unchanged (no re-encoding),
                # subtitle as a separate WebVTT file next to it.
                try:
                    write_vtt(os.path.join(self.out, vtt_name(name)), events, start)
                    shutil.copyfile(seg, os.path.join(self.out, name + ".tmp"))
                    os.replace(os.path.join(self.out, name + ".tmp"), os.path.join(self.out, name))
                except OSError as e:
                    log(f"Passing on piece failed ({os.path.basename(seg)}):", e)
                    first = True
                    continue
                if self.ev.is_set():
                    break
                if not self.pl.real and len(held) + 1 < CFG["start_segments"]:
                    # waiting screen stays a bit longer; this way Kodi starts with a buffer
                    held.append((name, dur, disc or first))
                    first = False
                    self.status["state"] = f"filling buffer ({len(held)}/{CFG['start_segments']})"
                    continue
                for h in held:
                    self.pl.add(*h)
                held = []
                self.pl.add(name, dur, disc or first)
                first = False
                self.status.update({"state": "running", "last_piece": name,
                                    "pass_on_s": round(time.time() - t0, 2),
                                    "processing_behind_s": round(time.time() - made, 1),
                                    "time": time.strftime("%H:%M:%S")})
                continue
            out_tmp = os.path.join(self.out, name + ".tmp")
            fix = self.audio_fix(seg)                   # v0.40
            p = self.burn(seg, ass_path, out_tmp, fix)
            if p.returncode != 0 and not self.ev.is_set():
                # v0.13: when burning in fails (e.g. a broken subtitle piece), still pass
                # the video on without subtitle instead of leaving a gap.
                log("Burning in failed, again without subtitle:", p.stderr[-300:].strip())
                p = self.burn(seg, None, out_tmp, fix)
            try:
                os.remove(ass_path)
            except OSError:
                pass
            if p.returncode != 0:
                log("Burning in failed:", p.stderr[-500:].strip())
                try:
                    os.remove(out_tmp)
                except OSError:
                    pass
                first = True       # mark the next piece as a new start
                continue
            if self.ev.is_set():
                break
            os.replace(out_tmp, os.path.join(self.out, name))
            _, out_dur = probe(os.path.join(self.out, name))
            if not 0.0 < out_dur <= 3.0 * CFG["seg_seconds"]:   # v0.40: odd times
                out_dur = dur
            self.pl.add(name, out_dur or dur, disc or first)
            first = False
            self.status.update({"state": "running", "last_piece": name,
                                "burn_s": round(time.time() - t0, 2),
                                # time between arrival of the piece and ready for Kodi
                                "processing_behind_s": round(time.time() - made, 1),
                                "time": time.strftime("%H:%M:%S")})

    def audio_fix(self, seg):
        """v0.40: does this piece have the same audio format as what Kodi already got?
        Yes: None (just copy). No (the channel changed, or the waiting screen could not
        reproduce the format): (audio_args, mux_args) to convert the audio.
        This way Kodi never gets MP2/AC3 data while it expects AAC (or the other way round)."""
        k = self.kodi_fmt
        if not k or not k.get("a"):
            return None
        f = seg_format(seg)
        name = self.ch["name"]
        if f.get("v") and k.get("v") and f["v"] != k["v"] and ("v", f["v"]) not in self.fmt_logged:
            self.fmt_logged.add(("v", f["v"]))
            self.status["video_format_change"] = f"{k['v']} -> {f['v']}"
            log(f"[{name}] NOTE: the channel's video is {f['v']}, Kodi got {k['v']}. "
                "If the picture stalls or stops: zap to another channel and back.")
        if (f.get("w") and k.get("w") and (f["w"], f["h"]) != (k["w"], k["h"])
                and ("s", f["w"], f["h"]) not in self.fmt_logged):
            self.fmt_logged.add(("s", f["w"], f["h"]))
            log(f"[{name}] the channel's picture size is {f['w']}x{f['h']} "
                f"(waiting screen {k['w']}x{k['h']})")
        a = f.get("a")
        if not a or audio_family(a) == audio_family(k["a"]):
            return None
        target = {"a": k["a"], "ar": k.get("ar", 48000), "ac": k.get("ac", 2)}
        if ("a", a) not in self.fmt_logged:
            self.fmt_logged.add(("a", a))
            self.status["audio_converted"] = f"{a} -> {k['a']}"
            log(f"[{name}] the channel's audio is {a}, Kodi got {k['a']}: "
                f"converting to {k['a']} (otherwise Kodi may hang)")
        return audio_encode_args(target), audio_mux_args(target)

    def publish_dvb(self, seg, name, events, start, dur, disc, w0=None, w1=None):
        """v0.17: pass on a piece with DVB subtitle. Video and audio unchanged.
        v0.31: on our own continuous timeline. As long as the source runs smoothly,
        the shift stays the same (picture exactly as the source). When the time
        jumps in the source (reconnect, adverts, other region), the new piece
        simply connects: no gap or jump back for Kodi any more (= stalling picture)."""
        from dvbsub import write_sup
        if w0 is None:
            w0 = start
        if not getattr(self, "canvas", None):
            self.canvas = dvb_canvas(*video_size(seg))
            log(f"[{self.ch['name']}] DVB subtitle on canvas {self.canvas[0]}x{self.canvas[1]}")
        W, H = self.canvas
        sup = os.path.join(self.subs, name + ".sup")
        out_tmp = os.path.join(self.out, name + ".tmp")
        fix = self.audio_fix(seg)                       # v0.40
        a_args, m_args = fix if fix else (None, ())
        if w1 is None:
            w1 = start + dur
        frames = video_frames(seg)                      # v0.41
        v_first, v_last = (frames[0], frames[-1]) if frames else (None, None)
        with self.clock_lock:
            jump = None if self.offset is None else start + self.offset - self.clock
            if jump is None or disc or not (-1.0 < jump < 0.5):
                if jump is not None and self.n_real:
                    log(f"[{self.ch['name']}] timeline connected (source shifted {jump:+.2f} s)")
                # small margin: B-frames have a decode time slightly before their start
                self.offset = self.clock + 0.1 - start
            off = self.offset
            # v0.41: does this piece connect seamlessly to the previous one? Then keep showing
            # the sentence that runs over the border without a gap (no blink any more).
            prev, self.sub_cut = getattr(self, "sub_cut", None), None
            cut_in = None
            if (prev and not disc and abs(prev[0] - w0) < 1e-6 and abs(prev[2] - off) < 1e-6
                    and (v_first is None or v_first > prev[1] + SUB_CUT_GAP)):
                cut_in = prev[1] + SUB_CUT_GAP
            cut_out = None
            if v_last is not None and w0 < v_last + SUB_CUT_AFTER < w1 + 1.0:
                cut_out = v_last + SUB_CUT_AFTER
            events = boundary_events(events, w0, w1, cut_in, cut_out, frames)
            if cut_in is not None:
                self.status["seamless_borders"] = self.status.get("seamless_borders", 0) + 1
            try:
                write_sup(sup, events_to_blocks(events), W, H, dvb_style(), t_clear_at=w0,
                          t_end=w1)
                p = mux_dvb(seg, sup, out_tmp, off, a_args, m_args)
                if p.returncode != 0 and not self.ev.is_set():
                    log("DVB subtitle failed, piece without text:", p.stderr[-300:].strip())
                    write_sup(sup, [], W, H, dvb_style(), t_clear_at=w0)
                    p = mux_dvb(seg, sup, out_tmp, off, a_args, m_args)
                if p.returncode != 0 and not self.ev.is_set() and self.kodi_fmt:
                    # v0.40: often broken audio in the source ('sample rate not set'). Then silence,
                    # in the format Kodi got, instead of losing the whole piece.
                    self.status["pieces_silent"] = self.status.get("pieces_silent", 0) + 1
                    log(f"[{self.ch['name']}] audio of {os.path.basename(seg)} unusable: "
                        "piece passed on with silence")
                    p = mux_dvb(seg, sup, out_tmp, off, silence=(start, dur, self.kodi_fmt))
            except Exception as e:  # noqa: BLE001
                log("Making DVB subtitle failed:", e)
                return False
            finally:
                try:
                    os.remove(sup)
                except OSError:
                    pass
            if p.returncode != 0 or self.ev.is_set():
                if p.returncode != 0:
                    log("Passing on piece failed:", p.stderr[-300:].strip())
                try:
                    os.remove(out_tmp)
                except OSError:
                    pass
                return False
            os.replace(out_tmp, os.path.join(self.out, name))
            self.pl.add(name, dur, disc)
            if self.t_first is None:
                self.t_first = time.time()
            self.clock = max(self.clock, start + dur + off)
            self.n_real += 1
            if cut_out is not None:
                self.sub_cut = (w1, cut_out, off)         # v0.41: for the next piece
        return True

    def check_lead(self):
        """v0.31: warn when Kodi has almost no video ready any more (= stalling)."""
        lead = self.lead()
        if lead is None:
            return
        self.status["buffer_s"] = round(lead, 1)
        self.lead_min = lead if self.lead_min is None else min(self.lead_min, lead)
        if lead < 3.0 and self.n_real > CFG["buffer_segments"]:
            self.status["buffer_almost_empty"] += 1
            if time.time() - self.last_low_log > 20:
                self.last_low_log = time.time()
                log(f"[{self.ch['name']}] BUFFER ALMOST EMPTY (~{lead:.1f} s ready for Kodi): "
                    "the picture may stall now. Cause: processing or source too slow.")

    def monitor_loop(self):
        """v0.31: one overview line in the log every minute, to explain stalls."""
        last = time.time()
        while not self.ev.wait(5):
            if self.pl.real and dvb_mode():
                self.check_lead()
            if time.time() - last < 60:
                continue
            last = time.time()
            ai, self.ai_times = self.ai_times, []
            g = gpu_now()
            lead_min, self.lead_min = self.lead_min, None
            parts = [f"{len(ai)} pieces"]
            lags, self.lag_times = self.lag_times, []
            if lags:
                parts.append(f"subtitle avg. {sum(lags) / len(lags):.1f} s behind (max {max(lags):.1f})")
            if ai:
                parts.append(f"AI avg. {sum(ai) / len(ai):.1f} s, max {max(ai):.1f} s")
            if lead_min is not None:
                parts.append(f"buffer min. {lead_min:.0f} s")
            now = dict(self.tr.stats)
            prev = self.asr_prev or {}
            self.asr_prev = now
            d = {k: now.get(k, 0) - prev.get(k, 0) for k in ("asr", "silence", "errors")}
            parts.append(f"speech recognition {d['asr']}x, silence {d['silence']}x"
                         + (f", errors {d['errors']}x" if d["errors"] else ""))
            self.status["speech_recognition"] = now
            parts.append(f"source interrupted {self.status['source_interruptions']}x, "
                         f"time jumps {self.status['source_time_jumps']}x, "
                         f"buffer almost empty {self.status['buffer_almost_empty']}x")
            if g:
                parts.append(f"GPU {g['load_pct']}% {g['temp_c']}°C "
                             f"{g['clock_mhz']}/{g['clock_max_mhz']} MHz")
                self.status["gpu"] = g
            log(f"[{self.ch['name']}] Overview: " + ", ".join(parts))

    def burn(self, seg, ass_path, out_tmp, fix=None):
        vf = []
        if CFG["deinterlace"]:
            vf.append("yadif=mode=send_frame:deint=interlaced")
        if ass_path:
            vf.append(f"subtitles=filename={ass_path}")
        vf.append("format=yuv420p")
        cmd = (["ffmpeg", "-v", "error", "-nostdin", "-y", "-copyts", "-i", seg,
                "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(vf)]
               + video_args()
               + (fix[0] if fix else ["-c:a", "copy"])            # v0.40
               + ["-muxdelay", "0", "-muxpreload", "0"] + (list(fix[1]) if fix else [])
               + ["-f", "mpegts", out_tmp])
        return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------- changes from the web page
class NoASR:
    """Placeholder when the speech recognition cannot start (e.g. no address yet).
    The stream still works (without subtitles); fix it on the web page."""
    name = "none"

    def __init__(self, reason):
        self.reason = reason

    def describe(self):
        return f"not working: {self.reason}"

    def recognize(self, audio, task="transcribe", language=None, prompt=None):
        raise RuntimeError(f"speech recognition not set up ({self.reason})")


def apply_web_changes(keys):
    """Make changed engine and language settings active without a restart.
    Returns an error text (the web page then keeps the old values) or None."""
    import settings
    tr = MANAGER.tr
    if keys & settings.ASR_KEYS:
        from asr import make_engine
        try:
            eng = make_engine(CFG)
        except Exception as e:  # noqa: BLE001
            return f"Speech recognition cannot start: {e}"
        with tr.lock:
            tr.engine = eng
            tr.fails, tr.down_until, tr.last_error = 0, 0.0, None
        log(f"Speech recognition changed: {eng.name} - {eng.describe()}")
    if keys & settings.TRANSLATOR_KEYS:
        from translate import make_translator
        new = make_translator(CFG, log)
        new.refresh()
        MANAGER.lt = new
        if MANAGER.active:
            MANAGER.active.lt = new
        log(f"Translator changed: {new.name} - {new.describe()} (target language {CFG['dst_lang']})")
        if "dst_lang" in keys:
            log("Target language changed: the subtitle track label changes at the next zap")
    if keys & settings.LANGUAGE_KEYS:
        with tr.lock:
            tr.set_channel(None)
        log(f"Languages changed: source {CFG['src_lang']}, allowed "
            f"{', '.join(sorted(CFG['lang_allowlist'])) if CFG['lang_allowlist'] else 'all'}")
    return None


def problems():
    """Short texts for the status page when something does not work."""
    out = []
    tr, lt = MANAGER.tr, MANAGER.lt
    if isinstance(tr.engine, NoASR):
        out.append(f"Speech recognition is not set up: {tr.engine.reason}. "
                   "Fill in the address on the 'Translation' page.")
    elif tr.last_error:
        out.append(f"Speech recognition error at {tr.last_error[0]}: {tr.last_error[1]}")
    if getattr(lt, "last_error", None):
        out.append(f"Translator problem at {lt.last_error[0]}: {lt.last_error[1]}")
    return out


# ---------------------------------------------------------------- zapping
class Manager:
    def __init__(self, chans, tr, lt):
        self.chans = {c["id"]: c for c in chans}
        self.order = [c["id"] for c in chans]
        self.tr, self.lt = tr, lt
        self.active = None
        self.last_seen = 0
        self.lock = threading.Lock()

    def touch(self, cid):
        with self.lock:
            if cid not in self.chans:
                return None
            self.last_seen = time.time()
            if self.active and self.active.ch["id"] == cid and not self.active.ev.is_set():
                return self.active
            if self.active and self.active.ch["id"] == cid and self.active.ev.is_set():
                log(f"Worker of {self.active.ch['name']} had stopped; restarting pipeline")
                self.active.stop()
                self.active = None
            if self.active:
                log(f"Zapping: stopping {self.active.ch['name']}")
                self.active.stop()
            log(f"Zapping: starting {self.chans[cid]['name']}")
            self.active = Pipeline(self.chans[cid], self.tr, self.lt)
            self.active.start()
            return self.active

    def seen(self, cid):
        """v0.29: the viewer of the TS stream is still alive. False = zapped away."""
        with self.lock:
            if (self.active and self.active.ch["id"] == cid
                    and not self.active.ev.is_set()):
                self.last_seen = time.time()
                return True
            return False

    def watchdog(self):
        while not STOP.is_set():
            time.sleep(5)
            with self.lock:
                if self.active and time.time() - self.last_seen > CFG["idle_stop"]:
                    log(f"Nobody is watching any more: stopping {self.active.ch['name']}")
                    self.active.stop()
                    self.active = None


MANAGER = None
TS_START_SEGMENTS = int(env("TS_START_SEGMENTS", "3"))
INTERN_UA = "M3U-Translator-Subtitle-internal"     # v0.29: recognises our own ffmpeg request (no zapping)


def sub_lang_code():
    """ISO 639-2 code of the target language for the subtitle track ('nl' -> 'dut')."""
    from languages import to_iso639_2
    return to_iso639_2(CFG["dst_lang"])


def use_ts_stream():
    """v0.29: continuous TS stream only with DVB subtitles or burning in.
    With SUB_MODE=track it stays HLS (inputstream.adaptive)."""
    return CFG["kodi_stream"] == "ts" and CFG["sub_mode"] != "track"

TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".m3u": "audio/x-mpegurl",
         ".ts": "video/mp2t", ".json": "application/json",
         ".vtt": "text/vtt; charset=utf-8"}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if not self.web_path():
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    # -- web interface (/, /settings, /api/...)
    PAGES = ("/", "/index.html", "/settings", "/settings/", "/instellingen")

    def is_browser(self):
        """A web browser asks for HTML; Kodi and ffmpeg do not."""
        return "text/html" in (self.headers.get("Accept") or "")

    def web_path(self):
        """Path for the web interface, or None. /settings/api/... also works.
        '/' is the web interface for a browser, and the channel list for Kodi."""
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/settings/api/"):
            path = path[len("/settings"):]
        if path in ("/", "/index.html") and not self.is_browser():
            return None
        if path in self.PAGES or path.startswith("/api/"):
            return path
        return None

    def status_data(self):
        a = MANAGER.active
        tr = MANAGER.tr
        return {
            "version": VERSION, "channels": len(MANAGER.order),
            "system": SYSTEM,
            "gpu_now": gpu_now(),
            "last_language_detection": {"language": tr.last_language,
                                        "certainty": round(tr.last_language_probability, 2)},
            "active": a.status if a else None}

    def web_auth(self):
        import settings
        if settings.check_auth(CFG, self.headers.get("Authorization")):
            return True
        body = b"Password required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="M3U Translator Subtitle", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return False

    def web_get(self, path):
        import settings
        if not self.web_auth():
            return
        if path in self.PAGES:
            return self.send(200, settings.page_html(), "text/html; charset=utf-8")
        if path == "/api/status":
            data = self.status_data()
            data["recent"] = recent_after(0, 5)[::-1]           # main page: newest 5
            data["target_language"] = CFG["dst_lang"]
            data["asr"] = MANAGER.tr.engine.name
            data["translator"] = MANAGER.lt.name
            data["problems"] = problems()
            return self.send(200, json.dumps(data, ensure_ascii=False), TYPES[".json"])
        if path == "/api/subtitles":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                after = int((q.get("after") or ["0"])[0])
            except ValueError:
                after = 0
            items = recent_after(after, 500)
            return self.send(200, json.dumps({"items": items, "last": _RECENT_ID[0]},
                                             ensure_ascii=False), TYPES[".json"])
        if path == "/api/channels":
            active = MANAGER.active.ch["id"] if MANAGER.active else None
            chans = [{"id": cid, "name": MANAGER.chans[cid]["name"],
                      "group": MANAGER.chans[cid].get("group") or "Translated",
                      "language": MANAGER.chans[cid].get("lang") or "",
                      "active": cid == active} for cid in MANAGER.order]
            return self.send(200, json.dumps({"channels": chans}, ensure_ascii=False),
                             TYPES[".json"])
        if path == "/api/settings":
            return self.send(200, json.dumps(settings.api_info(CFG), ensure_ascii=False),
                             TYPES[".json"])
        if path == "/api/preview.png":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            vals = {k: v[0] for k, v in q.items() if k in settings.FIELDS}
            text = (q.get("text") or [""])[0][:300] or " "
            old = (q.get("old") or [""])[0][:300]
            try:
                png = settings.preview_png(CFG, vals, text, old)
            except Exception as e:  # noqa: BLE001
                return self.send(500, f"preview failed: {e}")
            return self.send(200, png, "image/png")
        return self.send(404, "not found")

    def do_POST(self):
        import settings
        path = self.web_path()
        if not path:
            return self.send(404, "not found")
        if not self.web_auth():
            return
        if "application/json" not in (self.headers.get("Content-Type") or "") and \
                path != "/api/settings/reset":
            return self.send(415, "JSON only")
        if self.headers.get("Origin") and urllib.parse.urlparse(
                self.headers["Origin"]).netloc != (self.headers.get("Host") or ""):
            return self.send(403, "other website")      # no changes from a foreign site
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        if path == "/api/settings":
            try:
                vals = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(vals, dict):
                    raise ValueError("not an object")
            except ValueError:
                return self.send(400, json.dumps({"errors": {"_": "invalid data"}}),
                                 TYPES[".json"])
            errors = settings.save(CFG, vals, log, apply=apply_web_changes)
            if errors:
                return self.send(400, json.dumps({"errors": errors}, ensure_ascii=False),
                                 TYPES[".json"])
            return self.send(200, json.dumps({"values": settings.current(CFG)},
                                             ensure_ascii=False), TYPES[".json"])
        if path in ("/api/test/asr", "/api/test/translator"):
            try:
                vals = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                vals = {}
            test_cfg = settings.merged(CFG, vals if isinstance(vals, dict) else {})
            if path == "/api/test/asr":
                import asr
                ok, message = asr.test(test_cfg)
            else:
                import translate
                ok, message = translate.test(test_cfg, log)
            return self.send(200, json.dumps({"ok": ok, "message": message}, ensure_ascii=False),
                             TYPES[".json"])
        if path == "/api/settings/reset":
            settings.reset(CFG, log, apply=apply_web_changes)
            return self.send(200, json.dumps({"values": settings.current(CFG)},
                                             ensure_ascii=False), TYPES[".json"])
        return self.send(404, "not found")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        host = self.headers.get("Host") or f"localhost:{CFG['port']}"
        web = self.web_path()
        if web:
            return self.web_get(web)
        if path == "/favicon.ico":
            return self.send(204, b"")
        if path in ("/", "/kodi.m3u"):
            lines = ["#EXTM3U"]
            for cid in MANAGER.order:
                c = MANAGER.chans[cid]
                logo = f' tvg-logo="{c["logo"]}"' if c.get("logo") else ""
                lines.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-name="{c["name"]}"{logo} '
                             f'group-title="{c.get("group") or "Translated"}",{c["name"]} '
                             f'({CFG["dst_lang"].upper()})')
                if track_mode():
                    # v0.14: only inputstream.adaptive shows a separate subtitle track
                    # in HLS (Kodi's normal ffmpeg player ignores it).
                    lines.append("#KODIPROP:inputstream=inputstream.adaptive")
                    lines.append("#KODIPROP:inputstream.adaptive.manifest_type=hls")
                    lines.append("#KODIPROP:mimetype=application/vnd.apple.mpegurl")
                    lines.append(f"#KODIPROP:inputstream.adaptive.live_delay={CFG['live_delay']}")
                if use_ts_stream():
                    lines.append(f"http://{host}/ch/{cid}/live.ts")
                else:
                    lines.append(f"http://{host}/ch/{cid}/index.m3u8")
            return self.send(200, "\n".join(lines) + "\n", TYPES[".m3u"])
        if path in ("/status", "/status.json", "/live/status.json"):
            return self.send(200, json.dumps(self.status_data(), ensure_ascii=False, indent=1),
                             TYPES[".json"])
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "ch":
            cid, fname = parts[1], os.path.basename(parts[2])
            if fname == "live.ts":
                return self.stream_ts(cid)
            if self.headers.get("User-Agent") == INTERN_UA:
                # v0.29: ffmpeg of stream_ts: no zapping, only the active channel
                pipe = MANAGER.active
                if not pipe or pipe.ch["id"] != cid or pipe.ev.is_set():
                    return self.send(404, "channel not active")
            else:
                pipe = MANAGER.touch(cid)
            if not pipe:
                return self.send(404, "unknown channel")
            fpath = os.path.join(pipe.out, fname)
            if fname.endswith(".m3u8"):
                for _ in range(60):          # waiting screen is there after ~1 s
                    if os.path.exists(fpath):
                        break
                    time.sleep(0.25)
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
            except OSError:
                return self.send(404, "not ready yet")
            return self.send(200, data, TYPES.get(os.path.splitext(fname)[1],
                                                  "application/octet-stream"))
        return self.send(404, "not found")

    def stream_ts(self, cid):
        """v0.29: one continuous MPEG-TS stream for Kodi. ffmpeg reads our own
        HLS playlist and joins the pieces neatly (no stalls at the
        seams). The language label is set again; Kodi then shows e.g. 'Dutch'."""
        pipe = MANAGER.touch(cid)
        if not pipe:
            return self.send(404, "unknown channel")
        src = f"http://127.0.0.1:{CFG['port']}/ch/{cid}/index.m3u8"
        for _ in range(240):                       # waiting screen is there after ~1 s
            if os.path.exists(os.path.join(pipe.out, "index.m3u8")) or pipe.ev.is_set():
                break
            time.sleep(0.25)
        cmd = ["ffmpeg", "-v", "error", "-nostdin",
               "-live_start_index", str(-TS_START_SEGMENTS),
               "-max_reload", "100000", "-m3u8_hold_counters", "100000",
               "-user_agent", INTERN_UA, "-i", src,
               "-map", "0:v:0", "-map", "0:a:0?", "-map", "0:s:0?",
               "-c", "copy",
               "-metadata:s:s:0", f"language={sub_lang_code()}",
               "-muxdelay", "0", "-f", "mpegts", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            while not STOP.is_set():
                data = proc.stdout.read1(65536)
                if not data:
                    return                         # ffmpeg stopped
                if not MANAGER.seen(cid):
                    return                         # zapped to another channel
                self.wfile.write(data)
        except OSError:
            return                                 # Kodi closed the connection
        finally:
            proc.kill()
            proc.wait()

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass   # Kodi drops connections when zapping: no error message needed


# ---------------------------------------------------------------- system check
SYSTEM = {}


def system_check():
    """Versions in the log and in /status."""
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                             timeout=10).stdout.split("\n")[0]
        SYSTEM["ffmpeg"] = out.split(" ")[2] if out.startswith("ffmpeg version") else "?"
    except Exception:  # noqa: BLE001
        pass
    try:
        from vad import backend_name
        SYSTEM["voice_detection"] = backend_name()
    except Exception as e:  # noqa: BLE001
        SYSTEM["voice_detection"] = f"error: {e}"[:120]
    SYSTEM["speech_recognition"] = CFG["asr_engine"]
    SYSTEM["translator"] = CFG["tr_engine"]
    log("System: " + ", ".join(f"{k}={v}" for k, v in SYSTEM.items()))
    if SYSTEM.get("voice_detection") == "energy":
        log("NOTE: Silero voice detection not found; using a simple energy measurement "
            "(music is then more often treated as speech)")


# ---------------------------------------------------------------- start
def main():
    global MANAGER
    log(f"M3U Translator Subtitle {VERSION} start - language {CFG['src_lang']} -> {CFG['dst_lang']}")
    if 0 < CFG["merge_chars"] < 150:
        log(f"TIP: MERGE_MAX_CHARS={CFG['merge_chars']} is low; 200 is recommended "
            "(translate half sentences together, otherwise a negation can get lost)")
    if CFG["via_en_langs"]:
        log(f"Via English (VIA_ENGLISH_LANGS): {', '.join(sorted(CFG['via_en_langs']))}")
    if CFG["src_lang"] == "auto":
        log("Automatic language detection per channel: on" +
            (" (M3U language override on)" if CFG["use_playlist_lang"] else " (M3U language metadata is ignored)"))
        if CFG["lang_allowlist"] is None:
            log("Language filter: all languages allowed")
        else:
            log("Language filter: allowed = " + ", ".join(sorted(CFG["lang_allowlist"])))
        log(f"Language filter: at least {CFG['lang_accept_probability']:.2f} certainty; "
            f"fix the language from {CFG['lang_probability']:.2f} and {CFG['lang_votes']} votes")
        if CFG["lang_allowlist"] is not None:
            blocked_via = CFG["via_en_langs"] - CFG["lang_allowlist"]
            if blocked_via:
                log("NOTE: VIA_ENGLISH_LANGS contains languages blocked by LANGUAGE_ALLOWLIST: "
                    f"{', '.join(sorted(blocked_via))}")
    log("Subtitles: " + ("DVB subtitle in the stream (on/off in Kodi), video not re-encoded"
                         if dvb_mode() else
                         "separate track (WebVTT), video not re-encoded, Kodi via inputstream.adaptive"
                         if track_mode() else "burned in with " + CFG["encoder"]))
    log(f"Pieces: {CFG['seg_seconds']} s, overlap {CFG['overlap']} s, "
        f"language re-check every {CFG['lang_recheck']} pieces; no speech recognition during "
        f"silence or music (less than {CFG['min_speech']:.1f} s speech)")
    log(f"Audio buffer: HLS list={CFG['ingest_list_size']}, "
        f"extra keep margin={CFG['ingest_delete_threshold']}"
        + (f", from {CFG['max_backlog']} pieces on without new subtitles"
           if CFG["max_backlog"] > 0 else ", nothing is ever skipped"))
    # report illogical settings right away
    if CFG["overlap"] >= CFG["seg_seconds"]:
        log("NOTE: OVERLAP_SECONDS must be smaller than SEGMENT_SECONDS; overlap set to 2")
        CFG["overlap"] = 2.0
    if CFG["max_backlog"] and CFG["max_backlog"] <= CFG["queue_warn_segments"]:
        log("NOTE: MAX_BACKLOG is smaller than QUEUE_WARN_SEGMENTS; pieces will be skipped quickly")
    import settings
    settings.load(CFG, log)
    system_check()
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found")
    chans = load_channels()
    if not chans:
        sys.exit("No channels found: put .m3u files in the channels/ folder")
    log(f"{len(chans)} channels loaded")
    for d in (CFG["work"], CFG["out"]):
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            shutil.rmtree(fp, ignore_errors=True) if os.path.isdir(fp) else os.remove(fp)
    _load_res()
    for size in ("1920x1080", "1280x720"):          # prepare the most common formats
        placeholder_for("waiting", size)
        placeholder_for("offline", size)
    if _RES:
        log(f"{len(_RES)} channel formats known from {_RES_FILE}")

    try:
        tr = Transcriber()
    except Exception as e:  # noqa: BLE001
        log(f"NOTE: speech recognition (ASR_ENGINE={CFG['asr_engine']}) cannot start: {e}. "
            "The channels still work, without subtitles. Set it up on the web interface.")
        tr = Transcriber(NoASR(str(e)))
    log(f"Speech recognition: {tr.engine.name} - {tr.engine.describe()}")
    from translate import make_translator
    lt = make_translator(CFG, log)
    log(f"Translator: {lt.name} - {lt.describe()}")
    lt.refresh()
    MANAGER = Manager(chans, tr, lt)

    def stop(*_):
        log("Stopping...")
        STOP.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    threading.Thread(target=MANAGER.watchdog, daemon=True).start()
    srv = Server(("0.0.0.0", CFG["port"]), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"Ready. In Kodi use: http://IP-OF-THIS-PC:{CFG['port']}/kodi.m3u")
    log(f"Web interface: http://IP-OF-THIS-PC:{CFG['port']}/"
        + ("" if CFG["web_password"] else " (no password: set WEB_PASSWORD if the page "
           "is reachable from the internet)"))
    while not STOP.is_set():
        time.sleep(1)
    if MANAGER.active:
        MANAGER.active.stop()
    srv.shutdown()


if __name__ == "__main__":
    main()
