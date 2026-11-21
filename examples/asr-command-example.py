#!/usr/bin/env python3
"""Example of your own speech recognition as a program (ASR_ENGINE=command).

Use in .env:
    ASR_ENGINE=command
    ASR_COMMAND=python3 /config/examples/asr-command-example.py

For every piece the program is called with the path to a WAV file
(16 kHz, mono, 16-bit) as the last argument. Extra information is in the environment:
    ASR_LANGUAGE   language code when known ("en"), otherwise empty
    ASR_TASK       "transcribe" or "translate" (to English)
    ASR_PROMPT     previous text (only with ASR_PROMPT_CONTEXT=1)
The program writes JSON to stdout (see asr-plugin-example.py) or plain text.
Exit code 0 = success, 3 = "translate" not possible, anything else = error.

A program starts again for every piece. When a model has to stay in memory,
use ASR_ENGINE=plugin or a server (ASR_ENGINE=http) instead.

This example recognises no real words: it only reports how long the piece is.
"""
import json
import os
import sys
import wave

if os.environ.get("ASR_TASK") == "translate":
    sys.exit(3)

with wave.open(sys.argv[-1]) as w:
    seconds = w.getnframes() / w.getframerate()

print(json.dumps({
    "language": os.environ.get("ASR_LANGUAGE") or "en",
    "segments": [{"start": 0.0, "end": seconds,
                  "text": f"Test sentence of {seconds:.0f} seconds."}],
}))
