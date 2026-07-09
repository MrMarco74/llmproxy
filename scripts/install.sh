#!/bin/bash
# install.sh — Server-Installer für den Proxy-Host (läuft als root)
# Proxy läuft auf dem Proxy-Host (immer an), Ollama/ComfyUI bleiben auf dem GPU-Host — siehe install_gpu_agent.sh
# Verwendung lokal: rsync -av . root@<proxy-host>:/tmp/llmproxy-deploy/ && ssh root@<proxy-host> 'bash /tmp/llmproxy-deploy/install.sh'
set -e

INSTALL_DIR="/opt/llmproxy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== llmproxy Server-Installer ==="
echo "Install-Verzeichnis: $INSTALL_DIR"
echo ""

mkdir -p "$INSTALL_DIR"

# Repo-Struktur 1:1 nach /opt/llmproxy spiegeln (Docker-Build braucht dieselben
# relativen Pfade wie im Repo, siehe dashboard/Dockerfile COPY-Zeilen).
echo "→ Kopiere Proxy-Dateien …"
cp -r "$REPO_DIR/proxy"     "$INSTALL_DIR/"
cp -r "$REPO_DIR/monitor"   "$INSTALL_DIR/"
cp -r "$REPO_DIR/scripts"   "$INSTALL_DIR/"
cp -r "$REPO_DIR/assets"    "$INSTALL_DIR/"
cp -r "$REPO_DIR/dashboard" "$INSTALL_DIR/"
cp "$REPO_DIR/requirements.txt"   "$INSTALL_DIR/requirements.txt"
cp "$REPO_DIR/docker-compose.yml" "$INSTALL_DIR/docker-compose.yml"

# Konfig-YAMLs flach nach INSTALL_DIR (CONFIG_DIR in llmproxy.py), nur wenn noch
# nicht vorhanden (keine Überschreibung bestehender Live-Configs!)
for f in clients.yaml frontier.yaml routing.yaml eviction.yaml notifications.yaml logging.yaml; do
    if [ -f "$REPO_DIR/config/$f" ] && [ ! -f "$INSTALL_DIR/$f" ]; then
        echo "→ Kopiere $f (neu) …"
        cp "$REPO_DIR/config/$f" "$INSTALL_DIR/$f"
    elif [ -f "$REPO_DIR/config/$f" ]; then
        echo "→ $f existiert bereits — nicht überschrieben (manuell prüfen)"
    fi
done

# Python-Abhängigkeiten
echo "→ Installiere Python-Abhängigkeiten …"
pip3 install --quiet --break-system-packages --ignore-installed \
  -r "$INSTALL_DIR/requirements.txt" 2>/dev/null \
  || pip3 install --quiet --break-system-packages -r "$INSTALL_DIR/requirements.txt" || true

# systemd-Unit
echo "→ Installiere systemd-Unit …"
cp "$REPO_DIR/proxy/llmproxy.service" /etc/systemd/system/llmproxy.service
systemctl daemon-reload
systemctl enable llmproxy

# Neustart oder Erststart
if systemctl is-active --quiet llmproxy; then
    echo "→ Starte llmproxy neu …"
    systemctl restart llmproxy
else
    echo "→ Starte llmproxy …"
    systemctl start llmproxy
fi

sleep 2
echo ""
if systemctl is-active --quiet llmproxy; then
    echo "✓ llmproxy läuft."
    systemctl status llmproxy --no-pager -l | tail -5
else
    echo "✗ llmproxy hat nicht gestartet — prüfe: journalctl -u llmproxy -n 30"
    exit 1
fi

echo "→ Aktualisiere Dashboard-Container …"
cd "$INSTALL_DIR" && docker compose up -d --build
echo "✓ Setup vollständig abgeschlossen."
