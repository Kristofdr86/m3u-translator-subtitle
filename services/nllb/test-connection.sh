#!/usr/bin/env bash
# Tests the internal connection from the translator container to NLLB.
# Usage (in the project folder):  bash services/nllb/test-connection.sh
# Host test (optional): curl http://127.0.0.1:5100/health
cd "$(dirname "$0")/../.." || exit 1

try_url() {
  docker compose exec -T translator python3 -c "
import urllib.request,sys
try:
    urllib.request.urlopen('$1/health', timeout=4).read(); print('OK')
except Exception as e:
    print('NO')
" 2>/dev/null
}

echo "Translator container tries to reach NLLB..."
for url in http://nllb:5000; do
  r=$(try_url "$url")
  echo "  $url -> ${r:-NO}"
  if [ "$r" = "OK" ]; then
    echo
    echo "USE THIS ADDRESS in your .env as TRANSLATE_URL:"
    echo "  $url"
    exit 0
  fi
done
echo
echo "No connection. Check that nllb is running:  docker compose ps   and   docker compose logs nllb"
exit 1
