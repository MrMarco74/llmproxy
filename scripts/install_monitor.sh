#!/bin/bash
# install_monitor.sh — installiert LLM Monitor auf einem Linux-Client
# Verwendung: bash install_monitor.sh [--url http://<proxy-host>:11435]
set -e

# Wenn als root ausgeführt: echten User aus SUDO_USER ableiten
if [ "$EUID" -eq 0 ]; then
    if [ -z "$SUDO_USER" ]; then
        echo "⚠️  Als root ohne sudo ausgeführt — Desktop-Entry wird für root installiert."
        echo "   Besser: als normaler User ausführen (sudo wird automatisch für apt-get verwendet)."
        REAL_HOME="$HOME"
        REAL_USER="root"
    else
        REAL_USER="$SUDO_USER"
        REAL_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    fi
else
    REAL_HOME="$HOME"
    REAL_USER="$USER"
fi

INSTALL_DIR="$REAL_HOME/.local/share/llmproxy"
BIN_DIR="$REAL_HOME/.local/bin"
APPS_DIR="$REAL_HOME/.local/share/applications"
ICON_DIR="$REAL_HOME/.local/share/icons/hicolor/128x128/apps"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Läuft entweder aus dem gepackten Tarball (flach, alles in SCRIPT_DIR) oder
# direkt aus dem Repo-Checkout (scripts/install_monitor.sh, Quelldateien in ../monitor/)
if [ -f "$SCRIPT_DIR/llm_monitor.py" ]; then
    MONITOR_DIR="$SCRIPT_DIR"
else
    MONITOR_DIR="$(cd "$SCRIPT_DIR/../monitor" && pwd)"
fi

# Optionaler URL-Override
PROXY_URL="http://<proxy-host>:11435"
while [[ $# -gt 0 ]]; do
    case $1 in
        --url) PROXY_URL="$2"; shift 2 ;;
        *) echo "Unbekannte Option: $1"; exit 1 ;;
    esac
done

echo "=== LLM Monitor Installer ==="
echo "Install-Verzeichnis : $INSTALL_DIR"
echo "llmproxy URL        : $PROXY_URL"
echo ""

# Verzeichnisse anlegen
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"

# Python-Abhängigkeiten
echo "→ Prüfe Python-Abhängigkeiten …"
MISSING=()
python3 -c "import PyQt5" 2>/dev/null || MISSING+=("python3-pyqt5")
python3 -c "import httpx"  2>/dev/null || MISSING+=("python3-httpx")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  Fehlende Pakete: ${MISSING[*]}"
    if command -v apt-get &>/dev/null; then
        echo "  Installiere via apt-get …"
        sudo apt-get install -y "${MISSING[@]}" 2>/dev/null || \
            pip3 install --user --break-system-packages PyQt5 httpx
    else
        pip3 install --user PyQt5 httpx 2>/dev/null || \
            pip3 install --user --break-system-packages PyQt5 httpx
    fi
else
    echo "  PyQt5 und httpx bereits installiert — OK"
fi

# App-Datei kopieren
echo "→ Kopiere llm_monitor.py …"
cp "$MONITOR_DIR/llm_monitor.py" "$INSTALL_DIR/llm_monitor.py"

# Icon installieren in alle Hicolor-Standardgrößen
if [ -f "$MONITOR_DIR/assets/llm-monitor.png" ]; then
    echo "→ Installiere Icon …"
    SRC="$MONITOR_DIR/assets/llm-monitor.png"
    # Auch assets in INSTALL_DIR mitkopieren (für Python-Pfad-Fallback)
    mkdir -p "$INSTALL_DIR/assets"
    cp "$SRC" "$INSTALL_DIR/assets/llm-monitor.png"
    for SIZE in 16 32 48 64 128 256 512; do
        SDIR="$HOME/.local/share/icons/hicolor/${SIZE}x${SIZE}/apps"
        mkdir -p "$SDIR"
        if command -v convert &>/dev/null; then
            convert "$SRC" -resize "${SIZE}x${SIZE}" "$SDIR/llm-monitor.png" 2>/dev/null || \
                cp "$SRC" "$SDIR/llm-monitor.png"
        else
            cp "$SRC" "$SDIR/llm-monitor.png"
        fi
    done
    # Scalable slot (PNG als Fallback wenn kein SVG)
    mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
    cp "$SRC" "$HOME/.local/share/icons/hicolor/scalable/apps/llm-monitor.png" 2>/dev/null || true
fi

# Wrapper-Script
echo "→ Erstelle Wrapper-Script …"
cat > "$BIN_DIR/llm-monitor" <<EOF
#!/bin/bash
exec python3 "\$HOME/.local/share/llmproxy/llm_monitor.py" "\$@"
EOF
chmod +x "$BIN_DIR/llm-monitor"

# Startkonfig vorschreiben wenn --url angegeben
if [ "$PROXY_URL" != "http://<proxy-host>:11435" ]; then
    mkdir -p "$HOME/.config/llmproxy"
    cat > "$HOME/.config/llmproxy/monitor.conf" <<EOF
[monitor]
proxy_url = $PROXY_URL
EOF
    echo "→ Proxy-URL gespeichert: $PROXY_URL"
fi

# Desktop-Entry installieren
echo "→ Installiere Desktop-Entry …"
cp "$MONITOR_DIR/llm_monitor.desktop" "$APPS_DIR/llm-monitor.desktop"

# Desktop-Datenbank aktualisieren
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache &>/dev/null; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

# PATH-Check
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "⚠️  $BIN_DIR ist nicht in \$PATH."
    echo "   Füge folgende Zeile zu ~/.bashrc oder ~/.zshrc hinzu:"
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# Ownership korrigieren wenn als root ausgeführt
if [ "$EUID" -eq 0 ] && [ -n "$SUDO_USER" ]; then
    chown -R "$REAL_USER:" "$INSTALL_DIR" "$BIN_DIR/llm-monitor" "$APPS_DIR/llm-monitor.desktop" \
        "$REAL_HOME/.local/share/icons/hicolor" 2>/dev/null || true
    [ -f "$REAL_HOME/.config/llmproxy/monitor.conf" ] && \
        chown "$REAL_USER:" "$REAL_HOME/.config/llmproxy/monitor.conf" 2>/dev/null || true
fi

echo ""
echo "✓ LLM Monitor installiert!"
echo "  Starten über: Anwendungsmenü → 'LLM Monitor'"
echo "  Oder:         llm-monitor"
