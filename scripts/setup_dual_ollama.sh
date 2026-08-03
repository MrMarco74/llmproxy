#!/bin/bash
# setup_dual_ollama.sh - Rüstet den GPU-Host auf eine Dual-Ollama-Architektur für
# asymmetrisches Multi-GPU um. Beispiel unten: zwei unterschiedlich große GPUs
# (z.B. eine 24GB- und eine 12GB-Karte) — Werte/Kommentare an die eigene
# Hardware anpassen.
#
# NICHT ANWENDBAR auf dana's aktuelle Hardware (Stand 2026-07-29): dana ist
# jetzt Single-GPU (RTX 3080, 12GB) - die zweite Karte wurde ausgebaut. Dieses
# Skript nur bei Rückkehr zu echter Multi-GPU-Hardware erneut verwenden.

echo "Konfiguriere primären Ollama Dienst (beide GPUs kombiniert) auf Port 11434..."
# CUDA_VISIBLE_DEVICES=0,1 statt nur 0: Modelle, die nicht auf eine einzelne
# Karte solo passen, können hier gesplittet über beide GPUs geladen werden
# (kombiniertes VRAM). routing.yaml pinnt solche Modelle per target_gpu: 0
# fest hierher, damit sie nie auf ollama-gpu1 (GPU1-only, kleinere Karte)
# landen, wo sie garantiert per CUDA OOM fehlschlagen würden. OLLAMA_NUM_PARALLEL=1,
# da nur ein Client dahinter hängt - vermeidet 4x-KV-Cache-Speicherbedarf des
# Ollama-Defaults.
sudo mkdir -p /etc/systemd/system/ollama.service.d/
cat <<EOF | sudo tee /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="CUDA_VISIBLE_DEVICES=0,1"
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_NUM_PARALLEL=1"
EOF

echo "Konfiguriere sekundären Ollama Dienst (zweite GPU) auf Port 11438..."
cat <<EOF | sudo tee /etc/systemd/system/ollama-gpu1.service
[Unit]
Description=Ollama Service (GPU 1)
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=root
Group=root
Restart=always
RestartSec=3
Environment="OLLAMA_HOST=0.0.0.0:11438"
Environment="CUDA_VISIBLE_DEVICES=1"
# Teile das Modell-Verzeichnis mit der primären Instanz:
Environment="OLLAMA_MODELS=/usr/share/ollama/.ollama/models"

[Install]
WantedBy=default.target
EOF

echo "Lade systemd daemon neu..."
sudo systemctl daemon-reload

echo "Starte und aktiviere Dienste..."
sudo systemctl enable --now ollama
sudo systemctl restart ollama
sudo systemctl enable --now ollama-gpu1
sudo systemctl restart ollama-gpu1

echo "Warte 5 Sekunden, bis beide Dienste laufen..."
sleep 5
echo "Ollama GPU 0:"
curl -s http://localhost:11434/api/tags | grep -o '"name":"[^"]*"' | head -n 3
echo "Ollama GPU 1:"
curl -s http://localhost:11438/api/tags | grep -o '"name":"[^"]*"' | head -n 3

echo "Setup abgeschlossen!"
