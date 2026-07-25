#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
    echo "Fehler: .git/hooks Verzeichnis nicht gefunden." >&2
    exit 1
fi

cat << 'HOOK_EOF' > "$HOOKS_DIR/pre-push"
#!/bin/bash
# Auto-installiert von scripts/install-git-hooks.sh
REPO_ROOT="$(git rev-parse --show-toplevel)"
nohup "$REPO_ROOT/scripts/mirror_to_github.sh" >> "$REPO_ROOT/.git/mirror.log" 2>&1 &
disown
exit 0
HOOK_EOF

chmod +x "$HOOKS_DIR/pre-push"
echo "Git-Hooks erfolgreich installiert."
