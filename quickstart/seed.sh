#!/usr/bin/env sh
# Puts one dev.task bead in the store through the gateway, the same path the
# console uses, so the factory board has something to show.
set -eu
HUB="${HUB:-http://localhost:8001}"
HERE="$(cd "$(dirname "$0")" && pwd)"
curl -sS -f -X POST "$HUB/substrate/beads" \
  -H 'Content-Type: application/json' \
  -H 'X-Truline-Client: quickstart' -H 'X-Truline-Client-Type: human' -H 'X-Truline-Scopes: *' \
  --data @"$HERE/seed.json" \
  | python3 -c 'import sys,json; b=json.load(sys.stdin); print("created", b["namespace"]+"."+b["type"], b["id"], "in state", b["state"])'
echo "open http://localhost:8080/factory"
