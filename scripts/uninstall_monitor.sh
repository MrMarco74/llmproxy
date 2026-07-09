#!/bin/bash
# uninstall_monitor.sh — entfernt LLM Monitor vom System
set -e

echo "=== LLM Monitor Deinstallation ==="

rm -f  "$HOME/.local/bin/llm-monitor"
rm -f  "$HOME/.local/share/applications/llm-monitor.desktop"
rm -rf "$HOME/.local/share/llmproxy"
rm -f  "$HOME/.local/share/icons/hicolor/128x128/apps/llm-monitor.png"
for SIZE in 16 32 48 64; do
    rm -f "$HOME/.local/share/icons/hicolor/${SIZE}x${SIZE}/apps/llm-monitor.png"
done

if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi

echo "✓ LLM Monitor deinstalliert."
echo "  Konfiguration bleibt erhalten: ~/.config/llmproxy/"
echo "  Löschen mit: rm -rf ~/.config/llmproxy/"
