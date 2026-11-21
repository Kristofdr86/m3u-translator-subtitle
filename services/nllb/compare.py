#!/usr/bin/env python3
"""
Compare LibreTranslate with the NLLB translator, sentence by sentence (English -> Dutch).

Usage (in the project folder):
  python3 services/nllb/compare.py                       # finds the translator address in .env
  python3 services/nllb/compare.py http://IP:5000         # or give the address yourself
  python3 services/nllb/compare.py http://IP:5000 http://127.0.0.1:5100
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

SENTENCES = [
    "to the connecting rods and the crankshafts. Every component of the six-cylinder engine is built right here.",
    "Nearly 100 engines leave the plant every day. Every single one of them is subjected to a stress test",
    "and none are allowed to proceed to final assembly until they have passed it.",
    "The Prime Minister has said he will not resign, despite mounting pressure from his own party.",
    "Police are appealing for witnesses after a man was seriously injured in Leeds last night.",
    "It's been a cracking match so far, and the home side are well on top.",
    "Temperatures will drop sharply overnight, with frost likely in sheltered spots.",
]


def find_translator_in_env():
    env = Path(".env")
    if not env.exists():
        return None
    for line in env.read_text(errors="ignore").splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        if ("LIBRE" in key.upper() or "TRANSLATE" in key.upper()) and val.startswith("http"):
            return val
    return None


def translate(url, text):
    url = url.rstrip("/")
    if not url.endswith("/translate"):
        url += "/translate"
    data = json.dumps({"q": text, "source": "en", "target": "nl", "format": "text"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    return out.get("translatedText", "?"), time.time() - t


def main():
    libre = sys.argv[1] if len(sys.argv) > 1 else find_translator_in_env()
    nllb = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:5100"
    if not libre:
        print("Translator address not found in .env. Give it: python3 services/nllb/compare.py http://IP:5000")
        libre = None
    print(f"LibreTranslate: {libre or '(skipped)'}")
    print(f"NLLB:           {nllb}")
    print("=" * 76)
    tl = tn = 0.0
    for z in SENTENCES:
        print(f"EN    {z}")
        if libre:
            try:
                t, d = translate(libre, z)
                tl += d
                print(f"LIBRE {t}   ({d:.2f} s)")
            except Exception as e:
                print(f"LIBRE error: {e}")
        try:
            t, d = translate(nllb, z)
            tn += d
            print(f"NLLB  {t}   ({d:.2f} s)")
        except Exception as e:
            print(f"NLLB  error: {e}  (is 'nllb' running? see: docker compose logs nllb)")
        print("-" * 76)
    n = len(SENTENCES)
    if libre:
        print(f"Average LibreTranslate: {tl / n:.2f} s per sentence")
    print(f"Average NLLB:           {tn / n:.2f} s per sentence")


if __name__ == "__main__":
    main()
