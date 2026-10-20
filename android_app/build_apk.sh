#!/usr/bin/env bash
# Bygg APK-en for Servidor-appen (android_app).
#
#   ./build_apk.sh              bygg (debug, signert med debug-nokkelen)
#   ./build_apk.sh --clean      rydd build/ forst (bruk hvis noe er rart)
#   ./build_apk.sh --install    bygg og installer pa tilkoblet telefon
#
# Du kan kjore den herfra (android_app/) eller fra repo-roten:
#   bash android_app/build_apk.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

CLEAN=no
INSTALL=no
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=yes ;;
        --install) INSTALL=yes ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Ukjent valg: $arg (bruk --clean, --install eller --help)"; exit 2 ;;
    esac
done

echo "=================================================="
echo " Bygger Servidor-appen"
echo "=================================================="

# 1) Java
if ! command -v java >/dev/null 2>&1; then
    echo "FEIL: java mangler. Installer med:"
    echo "  sudo apt update && sudo apt install -y openjdk-21-jdk"
    exit 1
fi
echo "Java:        $(java -version 2>&1 | head -n 1)"

# 2) Android SDK
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}}"
if [ ! -d "$SDK" ]; then
    echo "FEIL: fant ikke Android SDK i $SDK"
    echo "Sett ANDROID_HOME, eller installer SDK via Android Studio."
    exit 1
fi
# local.properties gjor bygget uavhengig av miljovariabler
if [ ! -f local.properties ]; then
    echo "sdk.dir=$SDK" > local.properties
    echo "Skrev local.properties -> $SDK"
fi
echo "Android SDK: $SDK"

# 3) gradlew maa vaere kjorbar
chmod +x gradlew

# 4) Bygg
if [ "$CLEAN" = "yes" ]; then
    echo "Rydder build/ ..."
    ./gradlew clean --no-daemon -q
fi
echo "Kompilerer ... (forste gang kan ta flere minutter)"
if ! ./gradlew assembleDebug --no-daemon; then
    echo
    echo "BYGGET FEILET. Vanlige arsaker:"
    echo "  - Java for gammel/ny:  java -version  (trenger 17 eller nyere)"
    echo "  - SDK mangler platform: ls \"$SDK/platforms\""
    echo "  - Nettverksfeil: forste bygg laster ned avhengigheter"
    exit 1
fi

# 5) Finn APK-en (filnavnet kan variere mellom bygg)
APK_DIR="app/build/outputs/apk/debug"
APK="$(find "$APK_DIR" -maxdepth 1 -name '*.apk' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n 1 | cut -d' ' -f2-)"
if [ -z "$APK" ]; then
    echo "FEIL: bygget sa OK, men fant ingen .apk i $APK_DIR"
    exit 1
fi

echo
echo "=================================================="
echo " FERDIG"
echo "=================================================="
echo "APK:     $DIR/$APK"
echo "Storrelse: $(du -h "$APK" | cut -f1)"
AAPT="$(ls "$SDK"/build-tools/*/aapt2 2>/dev/null | tail -n 1 || true)"
if [ -n "$AAPT" ]; then
    "$AAPT" dump badging "$APK" 2>/dev/null | grep '^package:' | sed 's/^/Info:    /'
fi
echo
echo "Installer pa telefon:"
echo "  adb install -r \"$DIR/$APK\""

if [ "$INSTALL" = "yes" ]; then
    if ! command -v adb >/dev/null 2>&1; then
        echo "FEIL: adb mangler (ligger i $SDK/platform-tools)."
        exit 1
    fi
    if ! adb devices | grep -qE '^[^ ]+[[:space:]]+device$'; then
        echo
        echo "Ingen telefon funnet. Koble til med USB (USB-feilsoking) eller:"
        echo "  adb connect <telefonens-ip>:5555"
        exit 1
    fi
    echo
    echo "Installerer ..."
    adb install -r "$APK"
    echo "Ferdig. Apne 'Servidor AI' pa telefonen."
fi
