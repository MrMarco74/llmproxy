#!/bin/bash
# install_gpu_agent.sh — Installer für den Stats-Agent auf dem GPU-Host (läuft als root)
# Liefert GPU/CPU/RAM/Gaming-Mode-Daten an den auf dem Proxy-Host laufenden llmproxy.
# Verwendung lokal: rsync -av . root@<gpu-host>:/tmp/llmproxy-deploy/ && ssh root@<gpu-host> 'bash /tmp/llmproxy-deploy/install_gpu_agent.sh'
set -e

INSTALL_DIR="/opt/llmproxy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== gpu-agent Installer ==="
echo "Install-Verzeichnis: $INSTALL_DIR"
echo ""

mkdir -p "$INSTALL_DIR"

echo "→ Kopiere Agent-Dateien …"
cp "$REPO_DIR/gpu-agent/gpu-agent.py" "$INSTALL_DIR/gpu-agent.py"
cp "$REPO_DIR/gpu-agent/requirements-gpu-agent.txt" "$INSTALL_DIR/requirements-gpu-agent.txt"

echo "→ Installiere Python-Abhängigkeiten …"
pip3 install --quiet --break-system-packages --ignore-installed \
  -r "$INSTALL_DIR/requirements-gpu-agent.txt" 2>/dev/null \
  || pip3 install --quiet --break-system-packages -r "$INSTALL_DIR/requirements-gpu-agent.txt" || true

echo "→ Installiere systemd-Unit …"
cp "$REPO_DIR/gpu-agent/gpu-agent.service" /etc/systemd/system/gpu-agent.service
systemctl daemon-reload
systemctl enable gpu-agent

if systemctl is-active --quiet gpu-agent; then
    echo "→ Starte gpu-agent neu …"
    systemctl restart gpu-agent
else
    echo "→ Starte gpu-agent …"
    systemctl start gpu-agent
fi

sleep 2
echo ""
if systemctl is-active --quiet gpu-agent; then
    echo "✓ gpu-agent läuft."
    systemctl status gpu-agent --no-pager -l | tail -5
else
    echo "✗ gpu-agent hat nicht gestartet — prüfe: journalctl -u gpu-agent -n 30"
    exit 1
fi
