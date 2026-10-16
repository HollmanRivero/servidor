#!/usr/bin/env bash
#
# show-connection.sh - viser alt du trenger for aa koble mobilappen til serveren.
#
# Kjoer fra repo-roten:
#     bash deploy/show-connection.sh
#
# Skriver ut Tailscale-IP, LAN-IP, port, API-token og svarer paa /health.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/backend/.env"

env_value() {
    local line
    line="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 || true)"
    printf '%s' "${line#*=}"
}

PORT="$(env_value PORT)"
PORT="${PORT:-8000}"
TOKEN="$(env_value API_TOKEN)"
HOST="$(env_value HOST)"

TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo "== Nettverk =="
if [ -n "$TS_IP" ]; then
    echo "  Tailscale-IP  : $TS_IP   <-- bruk denne i appen"
else
    echo "  Tailscale-IP  : (ingen - er Tailscale oppe? kjoer: sudo tailscale up)"
fi
if [ -n "$LAN_IP" ]; then
    echo "  LAN-IP        : $LAN_IP   (kun for testing paa samme nett)"
fi
echo "  Port          : $PORT"
echo "  Lytt paa HOST : ${HOST:-0.0.0.0}"
echo
echo "== Skriv dette i appen (tannhjulet oppe til hoyre) =="
if [ -n "$TS_IP" ]; then
    echo "  URL del Servidor : http://$TS_IP:$PORT"
else
    echo "  URL del Servidor : http://<tailscale-ip>:$PORT"
fi
echo "  X-API-Token      : ${TOKEN:-<mangler - kjoer bash deploy/install.sh>}"
echo
echo "== Tjeneste =="
if command -v systemctl >/dev/null 2>&1; then
    echo "  servidor-super : $(systemctl is-active servidor-super 2>/dev/null || echo inaktiv)"
fi
echo
echo "== /health =="
CHECK_HOST="${HOST:-0.0.0.0}"
if [ "$CHECK_HOST" = "0.0.0.0" ] || [ -z "$CHECK_HOST" ]; then
    CHECK_HOST="127.0.0.1"
fi
if command -v curl >/dev/null 2>&1; then
    curl -s "http://$CHECK_HOST:$PORT/health" | python3 -m json.tool 2>/dev/null || \
        echo "  (fikk ikke svar - sjekk: journalctl -u servidor-super -n 40)"
fi
