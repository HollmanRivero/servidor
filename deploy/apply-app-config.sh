#!/usr/bin/env bash
#
# apply-app-config.sh - kjoer paa UTVIKLERMASKINEN.
#
# Leser deploy/app_config.json (skrevet av install.sh paa serveren) og skriver
# android_app/local.properties, slik at APK-en faar riktig server-URL og token
# uten at du maa skrive noe i appen.
#
#   bash deploy/apply-app-config.sh
#   cd android_app && ./gradlew assembleDebug

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$REPO_DIR/deploy/app_config.json"
PROPS="$REPO_DIR/android_app/local.properties"

if [ ! -f "$CFG" ]; then
    echo "FEIL: $CFG mangler." >&2
    echo "Kopier den fra serveren (den lages av deploy/install.sh) og proev igjen." >&2
    exit 1
fi

python3 - "$CFG" "$PROPS" <<'PY'
import json, pathlib, sys

cfg = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
props_path = pathlib.Path(sys.argv[2])

sdk_line = ""
if props_path.exists():
    for line in props_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("sdk.dir="):
            sdk_line = line
            break

lines = []
if sdk_line:
    lines.append(sdk_line)
lines.append(f"server.url={cfg['server_url']}")
lines.append(f"server.token={cfg['api_token']}")

props_path.parent.mkdir(parents=True, exist_ok=True)
props_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

print(f"Skrev {props_path}")
print(f"  server.url   = {cfg['server_url']}")
print(f"  server.token = <skjult> ({len(cfg['api_token'])} tegn)")
PY
