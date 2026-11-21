#!/bin/bash
# M3U Translator Subtitle - fetch channel lists from iptv-org; every list = its own group in Kodi
#
# Usage (in the project folder):  ./scripts/fetch-channels.sh <group-name> <source> [source ...]
#
#   ./scripts/fetch-channels.sh UK uk                 country (country code)
#   ./scripts/fetch-channels.sh Dutch lang:nld        language (language code, 3 letters)
#   ./scripts/fetch-channels.sh News news             category
#   ./scripts/fetch-channels.sh Spanish es ar         several sources in one group
#
# The list goes to channels/<group-name>.m3u. Remove a group: rm channels/<group-name>.m3u
# Codes: https://github.com/iptv-org/iptv/blob/master/PLAYLISTS.md
set -e
cd "$(dirname "$0")/.."
if [ $# -lt 2 ]; then
  sed -n '3,12p' "scripts/fetch-channels.sh" | sed 's/^# \{0,1\}//'
  exit 1
fi
group="$1"; shift
mkdir -p channels
tmp=$(mktemp)
echo "#EXTM3U" > "$tmp"
for src in "$@"; do
  case "$src" in
    lang:*) url="https://iptv-org.github.io/iptv/languages/${src#lang:}.m3u" ;;
    news|documentary|movies|music|sports|kids|general|entertainment|business|culture|education|lifestyle|religious|weather)
            url="https://iptv-org.github.io/iptv/categories/$src.m3u" ;;
    *)      url="https://iptv-org.github.io/iptv/countries/$src.m3u" ;;
  esac
  echo "Fetching: $url"
  if ! curl -fsSL "$url" | grep -v '^#EXTM3U' >> "$tmp"; then
    echo "  -> not found, skipped"
  fi
done
n=$(grep -c '^#EXTINF' "$tmp" || true)
if [ "$n" = "0" ]; then
  echo "No channels found, nothing changed."; rm -f "$tmp"; exit 1
fi
rm -f "channels/$group.m3u"
mv "$tmp" "channels/$group.m3u"
chmod 644 "channels/$group.m3u"
echo "Done: $n channels in group '$group' (channels/$group.m3u)"
docker compose restart translator
echo "Don't forget in Kodi: PVR & Live TV -> Clear data, and restart Kodi."
