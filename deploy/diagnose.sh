#!/usr/bin/env bash
#
# diagnose.sh - sjekker om serveren er oppe, og hvor den er tilgjengelig.
#
#     bash deploy/diagnose.sh
#
# Stopper ikke paa feil - den skal bare fortelle hva som er galt.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/backend/.env"

head1() { printf '\n\033[1;36m== %s\033[0m\n' "$1"; }
ok()    { printf '\033[1;32m  OK  \033[0m %s\n' "$1"; }
bad()   { printf '\033[1;31m  FEIL\033[0m %s\n' "$1"; }
meh()   { printf '\033[1;33m  ?   \033[0m %s\n' "$1"; }

env_value() {
    local l
    l="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 || true)"
    printf '%s' "${l#*=}"
}

PORT="$(env_value PORT)"
PORT="${PORT:-8000}"

head1 "1. Tailscale"
TS_IP=""
if command -v tailscale >/dev/null 2>&1; then
    TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
    if [ -n "$TS_IP" ]; then
        ok "Tailscale er oppe - IP: $TS_IP"
    else
        bad "Tailscale er installert, men ikke koblet til. Kjor: sudo tailscale up"
    fi
    tailscale status 2>&1 | head -n 5 | sed 's/^/         /'
else
    bad "Tailscale er ikke installert."
fi

head1 "2. Tjenesten servidor-super"
if command -v systemctl >/dev/null 2>&1; then
    STATE="$(systemctl is-active servidor-super 2>/dev/null || true)"
    if [ "$STATE" = "active" ]; then
        ok "kjorer"
    else
        bad "status='${STATE:-ukjent}'. Se: journalctl -u servidor-super -n 40"
    fi
else
    meh "systemctl finnes ikke"
fi

head1 "3. Lytter noe paa port $PORT?"
if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
        ok "ja"
        ss -ltn 2>/dev/null | grep ":${PORT} " | sed 's/^/         /'
    else
        bad "nei - serveren kjorer ikke"
    fi
elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep ":${PORT} " | sed 's/^/         /' || bad "nei"
else
    meh "verken ss eller netstat finnes"
fi

head1 "4. /health lokalt (127.0.0.1)"
curl -s --max-time 5 "http://127.0.0.1:${PORT}/health" | python3 -m json.tool 2>/dev/null \
    || bad "fikk ikke svar paa http://127.0.0.1:${PORT}/health"

head1 "5. /health over Tailscale"
if [ -n "$TS_IP" ]; then
    if curl -s --max-time 5 "http://${TS_IP}:${PORT}/health" >/dev/null 2>&1; then
        ok "svarer paa http://${TS_IP}:${PORT}"
    else
        bad "svarer IKKE paa http://${TS_IP}:${PORT} (brannmur? HOST?)"
    fi
else
    meh "hopper over - ingen Tailscale-IP"
fi

head1 "6. Nokler i backend/.env"
for k in API_TOKEN GEMINI_API_KEY DEEPSEEK_API_KEY; do
    v="$(env_value "$k")"
    if [ -n "$v" ]; then
        ok "$k satt (${#v} tegn)"
    else
        bad "$k mangler"
    fi
done

head1 "7. Dette skal du skrive i appen (tannhjulet)"
if [ -n "$TS_IP" ]; then
    printf '         URL del Servidor : \033[1;32mhttp://%s:%s\033[0m\n' "$TS_IP" "$PORT"
else
    printf '         URL del Servidor : \033[1;33m<tailscale-ip>:%s\033[0m\n' "$PORT"
fi
printf '         X-API-Token      : %s\n' "$(env_value API_TOKEN)"

head1 "8. Les appens feilmeldinger fra mobilen"
printf '         adb logcat | grep -iE "servidor|okhttp|retrofit|Connect|HttpException|401"\n'

printf '\n'
