#!/usr/bin/env bash
# Serve the VibeGuard dashboard + backend on this computer and publish it through a Cloudflare Tunnel.
# Everything stays here: the data directory is local, the tunnel only forwards requests to 127.0.0.1.
#
#   VG_HOSTNAME=vibeguard.livops.uz  VG_BASIC_AUTH=user:password  deploy/run-livops.sh
#
# Only SIMULATED/REPLAY demo data is served: the backend replays backup/demo-session (labelled replay) and
# no serial device is opened. Needs `cloudflared tunnel login` and `cloudflared tunnel create` done once
# (see DEPLOY_LIVOPS.md).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${VG_HOSTNAME:?set VG_HOSTNAME, e.g. vibeguard.livops.uz}"
: "${VG_BASIC_AUTH:?set VG_BASIC_AUTH=user:password (the public page is password protected)}"
TUNNEL="${VG_TUNNEL:-vibeguard}"
PORT="${VG_PORT:-8765}"
DATA="${VG_DATA_DIR:-$HOME/.vibeguard/livops-data}"
mkdir -p "$DATA"

cleanup() { kill "${BACKEND_PID:-}" "${TUNNEL_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

export VG_BASIC_AUTH
uv run vg-backend serve --data-dir "$DATA" --http-port "$PORT" --allow-host "$VG_HOSTNAME" \
    --backup backup/demo-session --loop &
BACKEND_PID=$!

for _ in $(seq 1 40); do  # wait until the backend answers (it needs the password)
  curl -s -o /dev/null -u "$VG_BASIC_AUTH" "http://127.0.0.1:$PORT/api/runtimes" && break
  sleep 0.5
done

cloudflared tunnel run --url "http://127.0.0.1:$PORT" "$TUNNEL" &
TUNNEL_PID=$!
echo "dashboard: https://$VG_HOSTNAME/dashboard/   (login: ${VG_BASIC_AUTH%%:*})"
wait
