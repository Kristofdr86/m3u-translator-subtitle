"""Speech recognition (ASR): connections to a speech recognition of your choice.

This program contains no speech model itself. You choose one with ASR_ENGINE:

  http     A server with an OpenAI-compatible API (/v1/audio/transcriptions).
           This can be your own server on your network or an online service.
  command  Your own program or script. Gets a WAV file, returns JSON.
  plugin   Your own Python file with a function transcribe(...). Stays loaded,
           so useful for a model that has to stay in memory.

Every connection returns the same (see normalize):
  {"language": "en", "language_probability": 0.97,
   "segments": [{"start": 0.0, "end": 2.4, "text": "Hello there.",
                 "avg_logprob": -0.2, "no_speech_prob": 0.01, "compression_ratio": 1.3}]}
Only "segments" (or "text") is required; the rest is used when it is there.
"""
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
import wave

import numpy as np

from languages import to_code

SR = 16000


class ASRUnsupported(Exception):
    """The chosen speech recognition cannot do this (e.g. translate to English)."""


def wav_bytes(audio):
    """float32 16 kHz mono -> WAV (16-bit PCM)."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return buf.getvalue()


_SENT = re.compile(r"(?<=[.!?…])\s+")


def split_sentences(text, a, b):
    """Text without times -> sentences, spread over [a, b] by number of characters."""
    parts = [p.strip() for p in _SENT.split(text.strip()) if p.strip()]
    if not parts:
        return []
    total = sum(len(p) for p in parts) or 1
    out, done = [], 0
    for p in parts:
        s = a + (b - a) * done / total
        done += len(p)
        e = a + (b - a) * done / total
        out.append({"start": s, "end": max(e, s + 0.3), "text": p})
    return out


def normalize(res, duration, span=None):
    """Answer of a connection -> (segments, language, certainty).
    span = (start, end) of the speech, for text without times."""
    if isinstance(res, str):
        res = {"text": res}
    if isinstance(res, list):
        res = {"segments": res}
    if not isinstance(res, dict):
        raise ValueError(f"unexpected answer from the speech recognition: {type(res).__name__}")
    lang = to_code(res.get("language"))
    prob = res.get("language_probability", res.get("language_prob"))
    try:
        prob = None if prob is None else float(prob)
    except (TypeError, ValueError):
        prob = None
    segs = []
    raw = res.get("segments")
    if isinstance(raw, list) and raw:
        for s in raw:
            if isinstance(s, (list, tuple)) and len(s) >= 3:
                s = {"start": s[0], "end": s[1], "text": s[2]}
            if not isinstance(s, dict):
                continue
            text = str(s.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(s.get("start", 0.0) or 0.0)
                end = float(s.get("end", start) or start)
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            seg = {"start": max(0.0, start), "end": max(end, start + 0.05), "text": text}
            for k in ("avg_logprob", "no_speech_prob", "compression_ratio"):
                if s.get(k) is not None:
                    try:
                        seg[k] = float(s[k])
                    except (TypeError, ValueError):
                        pass
            segs.append(seg)
    elif str(res.get("text") or "").strip():
        a, b = span if span else (0.0, duration)
        segs = split_sentences(str(res["text"]), a, max(b, a + 0.5))
    return segs, lang, prob


def _multipart(fields, file_field, filename, data, ctype):
    boundary = "----m3utranslator" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                  f"{v}\r\n".encode())
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
              f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
    out.write(data)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class HttpASR:
    """OpenAI-compatible speech API. ASR_URL is the base, e.g. http://server:8000/v1."""
    name = "http"

    def __init__(self, cfg):
        self.url = cfg["asr_url"].rstrip("/")
        if not self.url:
            raise ValueError("ASR_URL is empty (e.g. http://192.168.1.20:8000/v1)")
        self.key = cfg["asr_key"]
        self.model = cfg["asr_model"]
        self.timeout = cfg["asr_timeout"]
        self.fmt = cfg["asr_format"]
        self.can_translate = cfg["asr_translate"]

    def describe(self):
        return f"{self.url} (model {self.model or 'server default'})"

    def recognize(self, audio, task="transcribe", language=None, prompt=None):
        if task == "translate" and not self.can_translate:
            raise ASRUnsupported("translating to English is off (ASR_TRANSLATE=0)")
        path = "/audio/translations" if task == "translate" else "/audio/transcriptions"
        fields = {"response_format": self.fmt, "temperature": "0"}
        if self.model:
            fields["model"] = self.model
        if language and task != "translate":
            fields["language"] = language
        if self.fmt == "verbose_json":
            fields["timestamp_granularities[]"] = "segment"
        if prompt:
            fields["prompt"] = prompt
        body, ctype = _multipart(fields, "file", "piece.wav", wav_bytes(audio), "audio/wav")
        headers = {"Content-Type": ctype}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url + path, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            if task == "translate" and e.code in (400, 404, 405, 422, 501):
                self.can_translate = False
                raise ASRUnsupported(f"server cannot translate to English (HTTP {e.code})")
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        raw = raw.strip()
        if self.fmt in ("text", "srt", "vtt") or not raw.startswith(("{", "[")):
            return {"text": raw}
        return json.loads(raw)


class CommandASR:
    """Own program: gets the path to a WAV file (16 kHz mono) as the last
    argument, and the environment variables ASR_TASK, ASR_LANGUAGE and ASR_PROMPT.
    Writes JSON (see top) or plain text to stdout."""
    name = "command"

    def __init__(self, cfg):
        self.cmd = shlex.split(cfg["asr_command"])
        if not self.cmd:
            raise ValueError("ASR_COMMAND is empty")
        self.timeout = cfg["asr_timeout"]
        self.can_translate = cfg["asr_translate"]

    def describe(self):
        return " ".join(self.cmd)

    def recognize(self, audio, task="transcribe", language=None, prompt=None):
        if task == "translate" and not self.can_translate:
            raise ASRUnsupported("translating to English is off (ASR_TRANSLATE=0)")
        fd, path = tempfile.mkstemp(suffix=".wav", dir=os.environ.get("TMPDIR") or None)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(wav_bytes(audio))
            env = dict(os.environ, ASR_TASK=task, ASR_LANGUAGE=language or "",
                       ASR_PROMPT=prompt or "")
            p = subprocess.run(self.cmd + [path], capture_output=True, text=True,
                               timeout=self.timeout, env=env)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if p.returncode == 3 and task == "translate":
            self.can_translate = False
            raise ASRUnsupported("program cannot translate to English (exit code 3)")
        if p.returncode != 0:
            raise RuntimeError(f"exit code {p.returncode}: {(p.stderr or '').strip()[-300:]}")
        out = (p.stdout or "").strip()
        if out.startswith(("{", "[")):
            return json.loads(out)
        return {"text": out}


class PluginASR:
    """Own Python file (ASR_PLUGIN). Required:
        def transcribe(audio, sample_rate=16000, language=None, task="transcribe", prompt=None)
    Optional: def setup(): ...   (once at start, e.g. load a model)
              CAN_TRANSLATE = False   (when task="translate" is not possible)
    audio is numpy float32 mono. Returns a dict (see top), a list of segments
    or plain text."""
    name = "plugin"

    def __init__(self, cfg):
        path = cfg["asr_plugin"]
        if not path or not os.path.exists(path):
            raise ValueError(f"ASR_PLUGIN not found: {path or '(empty)'}")
        spec = importlib.util.spec_from_file_location("asr_plugin", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        if not hasattr(self.mod, "transcribe"):
            raise ValueError(f"{path} has no function transcribe(...)")
        if hasattr(self.mod, "setup"):
            self.mod.setup()
        self.path = path
        self.can_translate = cfg["asr_translate"] and getattr(self.mod, "CAN_TRANSLATE", True)

    def describe(self):
        return self.path

    def recognize(self, audio, task="transcribe", language=None, prompt=None):
        if task == "translate" and not self.can_translate:
            raise ASRUnsupported("plugin cannot translate to English")
        return self.mod.transcribe(audio, sample_rate=SR, language=language, task=task,
                                   prompt=prompt)


ENGINES = {"http": HttpASR, "openai": HttpASR, "command": CommandASR, "plugin": PluginASR}


def make_engine(cfg):
    kind = cfg["asr_engine"]
    if kind not in ENGINES:
        raise ValueError(f"ASR_ENGINE={kind} unknown; choose http, command or plugin")
    return ENGINES[kind](cfg)


def test(cfg):
    """For the web interface: does this speech recognition answer? Returns (ok, message).
    Sends 2 s of a quiet tone; an empty result is fine, an error is not."""
    try:
        eng = make_engine(cfg)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    t = np.arange(SR * 2) / SR
    audio = (0.05 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    try:
        res = eng.recognize(audio, task="transcribe", language=None)
        segs, lang, _ = normalize(res, 2.0)
    except Exception as e:  # noqa: BLE001
        return False, f"error: {e}"[:300]
    extra = f", language reported: {lang}" if lang else ", no language reported (automatic detection will not work)"
    return True, f"OK: {eng.describe()} answered{extra}"
