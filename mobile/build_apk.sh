#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=================================================="
echo "⚡ Bygger Super-Server Mobile APK"
echo "=================================================="

# 1. Sjekk Java
if ! command -v java &>/dev/null; then
    echo "❌ Java (JDK) ble ikke funnet."
    echo "👉 Installer JDK med: sudo apt update && sudo apt install -y openjdk-17-jdk"
    exit 1
fi
echo "✅ Java funnet: $(java -version 2>&1 | head -n 1)"

# 2. Finn eller sett opp Android SDK
if [ -z "$ANDROID_HOME" ]; then
    if [ -d "$HOME/Android/Sdk" ]; then
        export ANDROID_HOME="$HOME/Android/Sdk"
    elif [ -d "/usr/lib/android-sdk" ]; then
        export ANDROID_HOME="/usr/lib/android-sdk"
    fi
fi

if [ -n "$ANDROID_HOME" ]; then
    echo "✅ ANDROID_HOME: $ANDROID_HOME"
    echo "sdk.dir=$ANDROID_HOME" > local.properties
else
    echo "⚠️  Fant ikke ANDROID_HOME automatisk."
    echo "Hvis du har Android Studio eller SDK installert, sett: export ANDROID_HOME=~/Android/Sdk"
fi

# 3. Last ned gradlew hvis mangler
if [ ! -f "gradlew" ]; then
    if command -v gradle &>/dev/null; then
        echo "🔧 Genererer gradle wrapper..."
        gradle wrapper --gradle-version 8.2
    else
        echo "📥 Laster ned gradle wrapper..."
        curl -fsSL https://raw.githubusercontent.com/nicokaiser/gradle-wrapper/master/gradlew -o gradlew || true
        chmod +x gradlew || true
    fi
fi

chmod +x gradlew 2>/dev/null || true

# 4. Bygg APK
echo "🚀 Bygger APK med Gradle..."
if [ -f "./gradlew" ]; then
    ./gradlew assembleDebug
elif command -v gradle &>/dev/null; then
    gradle assembleDebug
else
    echo "❌ Verken ./gradlew eller gradle er tilgjengelig."
    echo "👉 Installer gradle med: sudo apt install -y gradle"
    exit 1
fi

APK_PATH="$DIR/app/build/outputs/apk/debug/app-debug.apk"

if [ -f "$APK_PATH" ]; then
    echo ""
    echo "=================================================="
    echo "🎉 GRATULERER! APK-EN ER FERDIG BYGGET!"
    echo "=================================================="
    echo "Filsti: $APK_PATH"
    echo ""

    # Sjekk ADB
    if command -v adb &>/dev/null; then
        DEVICES=$(adb devices | grep -v "List of devices" | grep "device$" || true)
        if [ -n "$DEVICES" ]; then
            echo "📱 Fant Android-mobil tilkoblet med USB!"
            read -p "Vil du installere appen på mobilen med en gang? (j/n): " ANS
            if [[ "$ANS" =~ ^[jJyY] ]]; then
                adb install -r "$APK_PATH"
                echo "✅ Appen er nå installert på mobilen din!"
                echo "Åpne 'Super-Server' på mobilen!"
            fi
        else
            echo "ℹ️  Tips: Koble mobilen til med USB og skru på USB-feilsøking, så kan du kjøre:"
            echo "   adb install -r $APK_PATH"
        fi
    fi

    # Tips for å sende til mobil over nett
    echo ""
    echo "📲 For å overføre til mobilen trådløst:"
    echo "1. Kopier filen eller send den via Tailscale / e-post / Google Drive."
    echo "2. Åpne og installer 'app-debug.apk' på mobilen."
    echo "3. Appen kobler seg automatisk til Super-Serveren på http://100.115.248.101:8000!"
else
    echo "❌ Kunne ikke finne APK-filen etter bygging."
    exit 1
fi
