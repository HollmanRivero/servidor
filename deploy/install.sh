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
SERVICE_NAME="servidor-super"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

cmd()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  + %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mFEIL: %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
    die "Kjoer skriptet som din vanlige bruker, ikke med sudo. Det spoer selv om sudo naar det trengs."
fi

# --------------------------------------------------------------- Python
cmd "Sjekker Python"
command -v python3 >/dev/null 2>&1 || die "python3 mangler. Kjoer: sudo apt install -y python3 python3-venv python3-pip"
python3 -c 'import venv' >/dev/null 2>&1 || die "python3-venv mangler. Kjoer: sudo apt install -y python3-venv"
ok "$(python3 --version)"

cmd "Lager virtuelt miljo i $VENV_DIR"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    ok "venv opprettet"
else
    ok "venv finnes allerede"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$BACKEND_DIR/requirements.txt"
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
    warn "$ENV_FILE fantes allerede - nokler i den beholdes uendret."
    warn "Er denne maskinen en KLOON av den gamle serveren, er de noklene kompromitterte."
    warn "Dette skriptet kan ikke se forskjell paa nye og gamle nokler - bytt dem manuelt."
fi

if ! grep -q '^API_TOKEN=.\+' "$ENV_FILE"; then
    if command -v openssl >/dev/null 2>&1; then
        TOKEN="$(openssl rand -hex 32)"
    else
        TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    printf 'API_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE"
    ok "Ny API_TOKEN generert"
else
    ok "API_TOKEN finnes allerede"
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
grep -q '^GEMINI_API_KEY=.\+'   "$ENV_FILE" || MISSING="$MISSING GEMINI_API_KEY"
grep -q '^DEEPSEEK_API_KEY=.\+' "$ENV_FILE" || MISSING="$MISSING DEEPSEEK_API_KEY"

if [ -n "$MISSING" ]; then
    warn "Mangler i .env:$MISSING"
    warn "Lag NYE nokler (de gamle har vaert eksponert via det aapne /file-endepunktet):"
    warn "  Gemini:   https://aistudio.google.com/apikey"
    warn "  DeepSeek: https://platform.deepseek.com/api_keys"
    warn "Legg dem inn slik:"
    warn "  printf 'GEMINI_API_KEY=<din nokkel>\\nDEEPSEEK_API_KEY=<din nokkel>\\n' >> $ENV_FILE"
else
    ok "Begge AI-noklene finnes"
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
After=network-online.target
Wants=network-online.target

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
    if sudo ufw status | grep -q '8000'; then
        ok "ufw har allerede en regel for port 8000"
    else
        sudo ufw allow in on tailscale0 to any port 8000 proto tcp >/dev/null
        ok "ufw: port 8000 aapnet KUN paa tailscale0"
    fi
else
    warn "ufw er ikke aktiv - hopper over (serveren lytter likevel kun der HOST tillater)"
fi

# --------------------------------------------------------------- oppsummering
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
API_TOKEN_VALUE="$(grep '^API_TOKEN=' "$ENV_FILE" | head -n1 | cut -d= -f2- || true)"

cmd "Ferdig"
cat <<SUMMARY
  Prosjekt    : $REPO_DIR
  Status      : sudo systemctl status $SERVICE_NAME
  Logg        : journalctl -u $SERVICE_NAME -f
  API-token   : $API_TOKEN_VALUE
SUMMARY

if [ -n "$TS_IP" ]; then
    printf '\n  \033[1;32mServeren naar deg paa: http://%s:8000\033[0m\n' "$TS_IP"
    cat <<SUMMARY

  Skriv dette inn i appen (tannhjulet oppe til hoyre):
      URL del Servidor : http://$TS_IP:8000
      X-API-Token      : (API-token over)

  Test fra telefonen: http://$TS_IP:8000/health
SUMMARY
else
    cat <<SUMMARY

  \033[1;33mTailscale er ikke satt opp enna.\033[0m Gjor dette, og kjor skriptet igjen:

      curl -fsSL https://tailscale.com/install.sh | sh
      sudo tailscale up          # apner nettleser for innlogging
      tailscale ip -4            # <- denne IP-en skal i appen
SUMMARY
fi

printf '\n'