#!/usr/bin/env bash
# Start Armory, check pages and redirect destinations, and verify authentication.
#
# Usage: bash scripts/smoke.sh
# Requires: python3, curl
set -euo pipefail

STATE="$(mktemp -d)"
PORT="${ARA_SMOKE_PORT:-4870}"
URL="http://127.0.0.1:${PORT}"

echo "[smoke] state=$STATE port=$PORT"

cleanup() {
  local rc=$?
  if [ -n "${ARMORY_PID:-}" ]; then
    kill "$ARMORY_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$ARMORY_PID" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL "$ARMORY_PID" 2>/dev/null || true
    wait "$ARMORY_PID" 2>/dev/null || true
  fi
  rm -rf "$STATE"
  exit $rc
}
trap cleanup EXIT INT TERM

PYTHONPATH="$(pwd)/src" python3 -m ara.cli armory \
  --state-root "$STATE" --bind 127.0.0.1 --port "$PORT" \
  >"$STATE/armory.stderr" 2>&1 &
ARMORY_PID=$!

for _ in $(seq 1 40); do
  if curl --connect-timeout 2 --max-time 8 -sf "$URL/healthz" >/dev/null 2>&1; then break; fi
  kill -0 "$ARMORY_PID" 2>/dev/null || {
    cat "$STATE/armory.stderr"
    exit 1
  }
  sleep 0.1
done

TOKEN="$(cat "$STATE/armory-token")"
if [ -z "$TOKEN" ]; then
  echo "[smoke] no token file produced"; exit 1
fi
echo "[smoke] armory up, token=${TOKEN:0:8}…"

fail=0
check_get() {
  local path="$1" expected="$2" target="${3:-}"
  local code location
  code=$(curl --connect-timeout 2 --max-time 8 -s -D "$STATE/headers" -o "$STATE/body" -w "%{http_code}" "$URL${path}?t=$TOKEN")
  echo "[smoke] GET $path -> $code"
  if [ "$code" != "$expected" ]; then fail=1; fi
  if [ -n "$target" ]; then
    location=$(awk 'tolower($1) == "location:" {print $2}' "$STATE/headers" | tr -d '\r')
    case "$location" in
      "$target"*) ;;
      *) echo "[smoke] FAIL: unexpected redirect for $path"; fail=1 ;;
    esac
    code=$(curl --connect-timeout 2 --max-time 8 -s -L --max-redirs 5 -o /dev/null -w "%{http_code}" "$URL${path}?t=$TOKEN")
    if [ "$code" != "200" ]; then
      echo "[smoke] FAIL: redirect target for $path returned $code"
      fail=1
    fi
  fi
}

for path in / /status /courtyard /rhema /armors /bulwark /settings /docs \
            /docs/overview /docs/armor-sets /docs/walls /docs/temper \
            /docs/absolute /docs/rhema /docs/overrides-blackholes \
            /docs/approvals /docs/audit /docs/sessions /docs/guard /docs/mitm /docs/cli; do
  check_get "$path" 200
done
check_get /absolute 303 '/rhema?tab=envelope'
check_get /overrides 303 '/allowlist?tab=allow'
check_get /blackholes 303 '/allowlist?tab=deny'
check_get /audit 303 '/courtyard?tab=live'
check_get /graph 303 '/courtyard?tab=graph'
check_get /approvals 303 '/courtyard?tab=approvals'
check_get /shadows 303 '/rhema?tab=shadows'
check_get /baselines 303 '/armors'

# Invalid browser credentials must reach login without protected content.
code=$(curl --connect-timeout 2 --max-time 8 -s -D "$STATE/headers" -o "$STATE/body" -w "%{http_code}" "$URL/status?t=garbage")
location=$(awk 'tolower($1) == "location:" {print $2}' "$STATE/headers" | tr -d '\r')
case "$code:$location" in
  302:/login\?next=*) echo "[smoke] invalid GET token -> login" ;;
  *) echo "[smoke] FAIL: invalid GET token did not redirect to login"; fail=1 ;;
esac
code=$(curl --connect-timeout 2 --max-time 8 -s -X POST -o /dev/null -w "%{http_code}" "$URL/absolute/save?t=garbage")
if [ "$code" != "401" ]; then
  echo "[smoke] FAIL: unauthenticated POST returned $code"
  fail=1
fi
if [ "$fail" -eq 0 ]; then
  echo "[smoke] ok"
else
  echo "[smoke] FAILED"
  exit 1
fi
