#!/bin/bash
# deploy_to_raspi.sh — Kopiert llmproxy auf den Proxy-Host (worker) und führt install.sh aus
set -e

TARGET_HOST="root@worker"
TARGET_DIR="/tmp/llmproxy-deploy"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "🚀 Synchronisiere llmproxy Dateien nach $TARGET_HOST:$TARGET_DIR..."
ssh "$TARGET_HOST" "mkdir -p $TARGET_DIR"
rsync -avz --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' "$REPO_DIR/" "$TARGET_HOST:$TARGET_DIR/"

ssh "$TARGET_HOST" "bash $TARGET_DIR/scripts/install.sh"

echo "✅ llmproxy Deployment abgeschlossen!"
