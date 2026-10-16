#!/usr/bin/env bash
#
# install.sh - setter opp Super-Server fra bunn paa en ny Ubuntu-maskin.
#
# Kjoeres fra repo-roten:
#     bash deploy/install.sh
#
# Skriptet er idempotent: det kan kjoeres om igjen uten aa odelegge noe.
# Det spoer om sudo underveis (systemd + brannmur), men skal IKKE kjoeres med sudo.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_DIR/backend"
VENV_DIR="$REPO_DIR/.venv"
ENV_FILE="$BACKEND_DIR/.env"
CONFIG_FILE="$REPO_DIR/deploy/config.local"
APP_CONFIG_FILE="$REPO_DIR/deploy/app_config.json"
ANDROID_DIR="$REPO_DIR/android_app"
ANDROID_LOCAL_PROPS="$ANDROID_DIR/local.properties"
SERVICE_NAME="servidor-super"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

cmd()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  + %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mFEIL: %s\033[0m\n' "$*" >&2; exit 1; }

# Les en verdi fra .env (tom streng hvis den ikke finnes)
get_env() {
    local line
    line="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 || true)"
    printf '%s' "${line#*=}"
}

# Sett/oppdater en verdi i .env uten aa roere alle andre linjer
set_env() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        awk -v k="$key" -v v="$value" '
            index($0, k "=") == 1 {
                if (!done) { print k "=" v; done = 1 }
                next
            }
            { print }
        ' "$ENV_FILE" > "$tmp"
    else
        cp "$ENV_FILE" "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
}

if [ "$(id -u)" -eq 0 ]; then
    die "Kjoer skriptet som din vanlige bruker, ikke med sudo. Det spoer selv om sudo naar det trengs."
fi

# Eksterne disker (f.eks. /run/media/...) monteres ofte med noexec, og da kan
# ikke .venv/bin/python kjore i det hele tatt.
if command -v findmnt >/dev/null 2>&1; then
    MOUNT_OPTS="$(findmnt -no OPTIONS --target "$REPO_DIR" 2>/dev/null || true)"
    case ",$MOUNT_OPTS," in
        *,noexec,*)
            die "Disken er montert med 'noexec' ($MOUNT_OPTS). Flytt repoet til hjemmemappen (f.eks. ~/Servidor_Super) og kjor skriptet der."
            ;;
    esac
fi

# ---------------------------------------------------- konfigurasjon (config.local)
# Standardverdier. Alt kan overstyres i deploy/config.local.
TAILSCALE_HOSTNAME="servidor-super"
TAILSCALE_AUTHKEY=""
GEMINI_API_KEY=""
DEEPSEEK_API_KEY=""
API_TOKEN=""
HOST=""
PORT="8000"
BIND_TAILSCALE_ONLY="no"
ENABLE_AGENT_SHELL="yes"

cmd "Leser konfigurasjon"
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
    ok "Leste $CONFIG_FILE"
else
    warn "Ingen $CONFIG_FILE funnet."
    warn "Lag den fra malen for aa fylle inn nokler automatisk:"
    warn "  cp deploy/config.local.example deploy/config.local && nano deploy/config.local"
fi
PORT="${PORT:-8000}"

# --------------------------------------------------------------- Tailscale
cmd "Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
    warn "Tailscale er ikke installert - installerer (krever sudo)..."
    curl -fsSL https://tailscale.com/install.sh | sudo sh || die "Kunne ikke installere Tailscale"
    ok "Tailscale installert"
else
    ok "Tailscale finnes ($(tailscale version 2>/dev/null | head -n1 || echo ukjent versjon))"
fi

TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
if [ -z "$TS_IP" ]; then
    if [ -n "$TAILSCALE_AUTHKEY" ]; then
        cmd "Kobler til tailnettet med auth key"
        sudo tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname="$TAILSCALE_HOSTNAME" || \
            warn "tailscale up feilet - sjekk auth key"
        TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
    else
        warn "Tailscale er ikke koblet til tailnettet."
        warn "  Kjor:  sudo tailscale up --hostname=$TAILSCALE_HOSTNAME"
        warn "  eller legg TAILSCALE_AUTHKEY=... i deploy/config.local og kjor skriptet igjen."
    fi
fi

if [ -n "$TS_IP" ]; then
    ok "Tailscale-IP: $TS_IP"
else
    warn "Ingen Tailscale-IP ennaa - app-konfigurasjonen faar plassholder."
fi

# Hvor skal serveren lytte? config.local vinner.
if [ -n "$HOST" ] && [ "$HOST" != "auto" ]; then
    ok "Lytt paa HOST=$HOST (fra config.local)"
elif [ "$BIND_TAILSCALE_ONLY" = "yes" ] && [ -n "$TS_IP" ]; then
    HOST="$TS_IP"
    ok "Lytt KUN paa Tailscale-adressen $HOST"
else
    HOST="0.0.0.0"
    ok "Lytt paa alle grensesnitt (0.0.0.0). Brannmur begrenser til tailscale0."
fi

# --------------------------------------------------------------- Python
cmd "Sjekker Python"
command -v python3 >/dev/null 2>&1 || die "python3 mangler. Kjoer: sudo apt install -y python3 python3-venv python3-pip"
python3 -c 'import venv' >/dev/null 2>&1 || die "python3-venv mangler. Kjoer: sudo apt install -y python3-venv"
ok "$(python3 --version)"

cmd "Lager virtuelt miljo i $VENV_DIR"
FORCE_FRESH="${1:-}"

# En .venv har absolutte stier innbakt og slutter aa virke naar mappen flyttes
# (f.eks. ny disk). Da feiler bin/pip med "bad interpreter". Lag den paa nytt.
if [ -d "$VENV_DIR" ]; then
    if [ "$FORCE_FRESH" = "--fresh" ]; then
        warn "--fresh: sletter og lager .venv paa nytt"
        rm -rf "$VENV_DIR"
    elif ! "$VENV_DIR/bin/pip" --version >/dev/null 2>&1; then
        warn "venv-en i $VENV_DIR virker ikke (flyttet disk?) - lager den paa nytt"
        rm -rf "$VENV_DIR"
    else
        ok "venv finnes og virker"
    fi
fi

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR" || die "Kunne ikke lage .venv (mangler python3-venv, eller disken er montert noexec?)"
    ok "venv opprettet"
fi
if [ ! -x "$VENV_DIR/bin/pip" ]; then
    die "Fikk ikke kjort $VENV_DIR/bin/pip. Prov: bash deploy/install.sh --fresh"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip || die "'pip install --upgrade pip' feilet (nettverk?)"
"$VENV_DIR/bin/pip" install --quiet -r "$BACKEND_DIR/requirements.txt" || die "'pip install -r backend/requirements.txt' feilet"
ok "Avhengigheter installert"

# --------------------------------------------------------------- git
# Agenten faar klone git-repoer inn i backend/projects/. Da maa git finnes.
cmd "Sjekker git (trengs for at agenten kan klone repoer)"
if command -v git >/dev/null 2>&1; then
    ok "$(git --version)"
else
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq
        sudo apt-get install -y git >/dev/null
        ok "git installert: $(git --version)"
    else
        warn "git mangler, og apt-get finnes ikke. Installer git manuelt, ellers kan ikke agenten klone."
    fi
fi

# --------------------------------------------------------------- prosjektmappen
# Agenten faar BARE lese/skrive/klone inne i backend/projects/. Er mappen laget
# av root (f.eks. etter en manuell test med sudo), kan ikke tjenestebrukeren
# skrive der - og da feiler bade kloning og fillagring.
cmd "Sjekker skriverettigheter i $BACKEND_DIR/projects"
mkdir -p "$BACKEND_DIR/projects"
if [ -w "$BACKEND_DIR/projects" ]; then
    ok "Prosjektmappen er skrivbar for $USER"
else
    sudo chown -R "$(id -u):$(id -g)" "$BACKEND_DIR/projects"
    if [ -w "$BACKEND_DIR/projects" ]; then
        ok "Rettighetene var feil - chown kjort, mappen er naa skrivbar"
    else
        warn "Kunne ikke rette rettighetene i $BACKEND_DIR/projects. Sjekk manuelt med: ls -ld $BACKEND_DIR/projects"
    fi
fi

# --------------------------------------------------------------- Tailscale
# Tailscale er en AVHENGIGHET: det er nettverket appen kobler seg til
# serveren gjennom. Skriptet installerer det hvis det mangler.
cmd "Sjekker Tailscale (avhengighet for app-tilkobling)"
if command -v tailscale >/dev/null 2>&1; then
    ok "Tailscale er installert ($(tailscale version 2>/dev/null | head -n1))"
else
    cmd "Installerer Tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
    ok "Tailscale installert"
fi

TS_STATE="$(tailscale status 2>/dev/null | head -n1 || true)"
case "$TS_STATE" in
    "Tailscale is stopped."*)
        warn "Tailscale er stoppet. Start og logg inn med: sudo tailscale up" ;;
    "Logged out."*)
        warn "Tailscale er ikke logget inn enna. Kjor: sudo tailscale up  (eller --authkey=...) og logg inn med samme konto som appen." ;;
    "")
        warn "Kunne ikke lese tailscale status - sjekk manuelt med: tailscale status" ;;
    *)
        ok "Tailscale er koblet til tailnettet" ;;
esac

# --------------------------------------------------------------- .env
cmd "Sjekker $ENV_FILE"
[ -f "$ENV_FILE" ] || : > "$ENV_FILE"

if [ -s "$ENV_FILE" ]; then
    warn "$ENV_FILE fantes allerede - verdier der brukes hvis config.local er tom."
    warn "Er denne maskinen en KLOON av den gamle serveren, maa noklene byttes manuelt."
fi

ENV_API_TOKEN="$(get_env API_TOKEN)"
ENV_GEMINI="$(get_env GEMINI_API_KEY)"
ENV_DEEPSEEK="$(get_env DEEPSEEK_API_KEY)"

FINAL_API_TOKEN="${API_TOKEN:-$ENV_API_TOKEN}"
if [ -z "$FINAL_API_TOKEN" ]; then
    if command -v openssl >/dev/null 2>&1; then
        FINAL_API_TOKEN="$(openssl rand -hex 32)"
    else
        FINAL_API_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    ok "Ny API_TOKEN generert"
else
    ok "Bruker eksisterende/oppgitt API_TOKEN"
fi

if ! grep -q '^JWT_SECRET=.\+' "$ENV_FILE"; then
    if command -v openssl >/dev/null 2>&1; then
        JWT="$(openssl rand -hex 32)"
    else
        JWT="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    printf 'JWT_SECRET=%s\n' "$JWT" >> "$ENV_FILE"
    ok "Ny JWT_SECRET generert"
else
    ok "JWT_SECRET finnes allerede"
fi

MISSING=""
if [ -n "$FINAL_GEMINI" ]; then
    set_env GEMINI_API_KEY "$FINAL_GEMINI"
else
    MISSING="$MISSING GEMINI_API_KEY"
fi
if [ -n "$FINAL_DEEPSEEK" ]; then
    set_env DEEPSEEK_API_KEY "$FINAL_DEEPSEEK"
else
    MISSING="$MISSING DEEPSEEK_API_KEY"
fi

if [ -n "$MISSING" ]; then
    warn "Mangler fortsatt i .env:$MISSING"
    warn "Lag NYE nokler (de gamle har vaert eksponert) og legg dem i deploy/config.local:"
    warn "  Gemini:   https://aistudio.google.com/apikey"
    warn "  DeepSeek: https://platform.deepseek.com/api_keys"
    warn "Deretter: bash deploy/install.sh"
else
    ok "Baade GEMINI_API_KEY og DEEPSEEK_API_KEY er satt"
fi

# Andre/nye nokler som listes i EXTRA_ENV_KEYS skrives rett inn i .env
if [ -n "${EXTRA_ENV_KEYS:-}" ]; then
    for extra_key in $EXTRA_ENV_KEYS; do
        extra_val="${!extra_key:-}"
        if [ -n "$extra_val" ]; then
            set_env "$extra_key" "$extra_val"
            ok "Satte $extra_key i .env"
        fi
    done
fi

# AI-agentenes shell-tilgang (se deploy/config.local.example)
set_env ENABLE_AGENT_SHELL "${ENABLE_AGENT_SHELL:-yes}"
if [ "${ENABLE_AGENT_SHELL:-yes}" = "yes" ]; then
    ok "AI-agentene har shell-tilgang i backend/projects/ (ENABLE_AGENT_SHELL=yes)"
else
    warn "Agent-shell er av (sett ENABLE_AGENT_SHELL=yes i config.local for aa slaa paa)"
fi

# --------------------------------------------------------------- GitHub-token
# Repoene er private. Baade 'git pull' paa serveren og agentens clone_repo
# trenger en token for aa slippe inn.
cmd "Sjekker GITHUB_TOKEN (trengs for private repoer)"
GITHUB_TOKEN_VALUE="$(grep '^GITHUB_TOKEN=.\+' "$ENV_FILE" | head -n1 | cut -d= -f2- || true)"
if [ -n "$GITHUB_TOKEN_VALUE" ]; then
    ok "GITHUB_TOKEN finnes"
    if command -v git >/dev/null 2>&1; then
        # Gjoer at 'git pull' i repoet virker uten passordsporsmaal. Tokenen
        # havner i ~/.gitconfig - samme bruker og maskin som .env.
        git config --global url."https://x-access-token:${GITHUB_TOKEN_VALUE}@github.com/".insteadOf "https://github.com/"
        ok "git bruker tokenen automatisk mot github.com (lagret i ~/.gitconfig)"
    fi
else
    warn "GITHUB_TOKEN mangler i $ENV_FILE"
    warn "  Private repoer kan ikke klones eller hentes uten den."
    warn "  Lag en fine-grained PAT med 'Contents: Read' og legg den inn:"
    warn "    printf 'GITHUB_TOKEN=<din token>\\n' >> $ENV_FILE"
    warn "    bash deploy/install.sh     # aktiverer den ogsaa for git"
fi

# --------------------------------------------------------------- systemd
cmd "Setter opp systemd-tjenesten '$SERVICE_NAME'"
if command -v systemctl >/dev/null 2>&1; then
    sudo tee "$SERVICE_FILE" >/dev/null <<UNIT
[Unit]
Description=Super-Server API (FastAPI, Gemini + DeepSeek)
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$BACKEND_DIR
ExecStart=$VENV_DIR/bin/python main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}.service" >/dev/null
    sudo systemctl restart "${SERVICE_NAME}.service"
    sleep 2
    if sudo systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        ok "Tjenesten kjorer"
    else
        warn "Tjenesten startet ikke. Sjekk loggen:  journalctl -u $SERVICE_NAME -n 40"
    fi
else
    warn "systemctl finnes ikke. Kjor serveren manuelt:"
    warn "  cd $BACKEND_DIR && $VENV_DIR/bin/python main.py"
fi

# --------------------------------------------------------------- brannmur
cmd "Brannmur"
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q 'Status: active'; then
    if sudo ufw status | grep -q "$PORT"; then
        ok "ufw har allerede en regel for port $PORT"
    else
        sudo ufw allow in on tailscale0 to any port "$PORT" proto tcp >/dev/null
        ok "ufw: port $PORT aapnet KUN paa tailscale0"
    fi
else
    warn "ufw er ikke aktiv - hopper over (serveren lytter likevel kun der HOST tillater)"
fi

# --------------------------------------------------------------- app-konfig
cmd "Skriver app-konfigurasjon"
if [ -n "$TS_IP" ]; then
    SERVER_URL="http://$TS_IP:$PORT"
else
    SERVER_URL="http://<tailscale-ip>:$PORT"
fi

cat > "$APP_CONFIG_FILE" <<JSON
{
  "server_url": "$SERVER_URL",
  "api_token": "$FINAL_API_TOKEN",
  "port": $PORT
}
JSON
ok "Skrev $APP_CONFIG_FILE"

if [ -d "$ANDROID_DIR" ]; then
    SDK_LINE=""
    if [ -f "$ANDROID_LOCAL_PROPS" ]; then
        SDK_LINE="$(grep -E '^sdk\.dir=' "$ANDROID_LOCAL_PROPS" || true)"
    fi
    {
        if [ -n "$SDK_LINE" ]; then printf '%s\n' "$SDK_LINE"; fi
        printf 'server.url=%s\n' "$SERVER_URL"
        printf 'server.token=%s\n' "$FINAL_API_TOKEN"
    } > "$ANDROID_LOCAL_PROPS"
    ok "Skrev $ANDROID_LOCAL_PROPS - APK-en bygges med riktig URL og token"
fi

# --------------------------------------------------------------- oppsummering
cmd "Ferdig"
cat <<SUMMARY
  Prosjekt    : $REPO_DIR
  Status      : sudo systemctl status $SERVICE_NAME
  Logg        : journalctl -u $SERVICE_NAME -f
  Lytter paa  : $HOST:$PORT
  API-token   : $FINAL_API_TOKEN
  App-konfig  : $APP_CONFIG_FILE
SUMMARY

if [ -n "$TS_IP" ]; then
    printf '\n  \033[1;32mServeren naar deg paa: %s\033[0m\n' "$SERVER_URL"
    cat <<SUMMARY

  Appen (android_app) er allerede satt opp med denne URL-en og tokenen.
  Bygg og installer:

      cd android_app && ./gradlew assembleDebug
      adb install -r app/build/outputs/apk/debug/app-debug.apk

  Bygger du paa en annen maskin: kopier deploy/app_config.json dit og kjor
      bash deploy/apply-app-config.sh

  Test fra telefonen: $SERVER_URL/health
SUMMARY

    CHECK_HOST="$HOST"
    if [ "$CHECK_HOST" = "0.0.0.0" ]; then CHECK_HOST="127.0.0.1"; fi
    if command -v curl >/dev/null 2>&1; then
        printf '\n  /health sier:\n'
        curl -s "http://${CHECK_HOST}:${PORT}/health" | python3 -m json.tool 2>/dev/null || \
            warn "Fikk ikke svar paa /health ennaa - sjekk: journalctl -u $SERVICE_NAME -n 40"
    fi
else
    cat <<SUMMARY

  \033[1;33mTailscale er ikke satt opp ennaa.\033[0m Gjor dette, og kjor skriptet igjen:

      curl -fsSL https://tailscale.com/install.sh | sh
      sudo tailscale up --hostname=$TAILSCALE_HOSTNAME
      tailscale ip -4            # <- denne IP-en skal i appen
SUMMARY
fi

printf '\n'