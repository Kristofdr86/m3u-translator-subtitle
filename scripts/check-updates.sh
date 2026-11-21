#!/usr/bin/env bash
# M3U Translator Subtitle - checks online whether there are newer versions of the pinned packages.
# NOTHING is changed. You only see what is new.
#
# Usage (in the project folder):  ./scripts/check-updates.sh
cd "$(dirname "$0")/.." || exit 1

python3 - <<'PY'
import json, re, urllib.request
from pathlib import Path

def pinned(dockerfile):
    """Pinned versions (name==version) from a Dockerfile."""
    try:
        txt = Path(dockerfile).read_text()
    except OSError:
        return {}
    return {m.group(1).lower(): m.group(2)
            for m in re.finditer(r"([A-Za-z0-9_.-]+)==([0-9][0-9A-Za-z.]*)", txt)}

def newest(pkg):
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=15) as r:
            return json.load(r)["info"]["version"]
    except Exception:
        return None

print("=" * 64)
print(" Versions: in use  ->  newest online (PyPI)")
print("=" * 64)
for title, df in (("Dockerfile (translator)", "Dockerfile"),
                  ("services/nllb/Dockerfile (optional)", "services/nllb/Dockerfile")):
    pins = pinned(df)
    if not pins:
        continue
    print(f"\n{title}")
    for p, cur in sorted(pins.items()):
        new = newest(p)
        if not new:
            print(f"  {p:15} {cur:10} -> (internet not reachable)")
            continue
        print(f"  {p:15} {cur:10} -> {new:10} {'up to date' if new == cur else 'NEW'}")
print()
print("Updating: change the version in the Dockerfile, test, and rebuild:")
print("  docker compose up -d --build")
PY
