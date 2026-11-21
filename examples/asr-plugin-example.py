"""Example of your own speech recognition as a plugin (ASR_ENGINE=plugin).

Use in .env:
    ASR_ENGINE=plugin
    ASR_PLUGIN=/config/examples/asr-plugin-example.py

This example does NOT recognise real words. It only shows "[speech 2.4 s]" where
someone speaks. This way you can test the whole chain (channel -> subtitle in Kodi)
without a speech model. Copy this file and replace transcribe() with your own model.

Rules for a plugin:
  * transcribe(audio, sample_rate=16000, language=None, task="transcribe", prompt=None)
      audio        numpy float32, mono, -1..1, by default 10 s (SEGMENT_SECONDS + overlap)
      language     ISO code ("en", "nl") when the channel language is known, otherwise None
      task         "transcribe" (write out) or "translate" (translate to English)
      prompt       previous text as context (only with ASR_PROMPT_CONTEXT=1), otherwise None
    Returns: a dict
      {"language": "en", "language_probability": 0.97,
       "segments": [{"start": 0.0, "end": 2.4, "text": "Good evening."}]}
    or only {"text": "..."} (the sentences are then spread over the speech automatically),
    or a list [(start, end, text), ...].
    "language" is needed for automatic language detection (SOURCE_LANG=auto).
  * setup()          optional, once at start (e.g. load a model).
  * CAN_TRANSLATE    optional; False when task="translate" is not possible.
The function is never called for two pieces at the same time.
"""
import numpy as np

CAN_TRANSLATE = False


def setup():
    # Load your model here, e.g. into memory or onto the GPU.
    pass


def transcribe(audio, sample_rate=16000, language=None, task="transcribe", prompt=None):
    # Simple measurement: blocks of 0.5 s that are loud enough count as speech.
    step = sample_rate // 2
    n = len(audio) // step
    loud = [float(np.sqrt(np.mean(audio[i * step:(i + 1) * step] ** 2))) > 0.01 for i in range(n)]
    segments, start = [], None
    for i, on in enumerate(loud + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            a, b = start * 0.5, i * 0.5
            segments.append({"start": a, "end": b, "text": f"[speech {b - a:.1f} s]."})
            start = None
    return {"language": language or "en", "language_probability": 1.0, "segments": segments}
