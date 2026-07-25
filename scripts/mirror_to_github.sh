#!/bin/bash
# Voller Snapshot-Resync des internen Repos (HEAD) auf den privaten
# GitHub-Mirror (github.com/MrMarco74/llmproxy). Mirrort den committeten
# Stand, nicht das Arbeitsverzeichnis -- unstaged/uncommittete lokale
# Änderungen landen nie im Mirror. Wird normalerweise automatisch über den
# pre-push-Hook angestoßen (s. scripts/install-git-hooks.sh), kann aber auch
# manuell aufgerufen werden.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIRROR_DIR="${MIRROR_DIR:-$HOME/Documents/github/llmproxy}"
LOG_PREFIX="[mirror_to_github]"

# Pfade, die NIE in den Mirror dürfen -- zusätzlich zu allem, was .gitignore
# ohnehin von HEAD fernhält.
EXCLUDE_PATTERNS=(
    ".env"
    "proxy/config/*.yaml"
    "proxy/config/tokens.yaml"
    "proxy/config/.admin_token"
    "*.db"
    "*.db-journal"
)

if [ ! -d "$MIRROR_DIR/.git" ]; then
    echo "$LOG_PREFIX FEHLER: $MIRROR_DIR ist kein Git-Repo. Abbruch." >&2
    exit 1
fi

cd "$SOURCE_ROOT"
COMMIT_SHA="$(git rev-parse --short HEAD)"
COMMIT_DATE="$(git log -1 --format=%cd --date=short)"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

git archive HEAD | tar -x -C "$TMP_DIR"

for pattern in "${EXCLUDE_PATTERNS[@]}"; do
    find "$TMP_DIR" -path "$TMP_DIR/$pattern" -delete 2>/dev/null || true
done

SECRET_PATTERN="BEGIN (RSA |OPENSSH |PGP )?PRIVATE KEY"
if grep -rIlE "$SECRET_PATTERN" "$TMP_DIR" >/dev/null 2>&1; then
    echo "$LOG_PREFIX WARNUNG: privater Schlüssel gefunden, Sync abgebrochen:" >&2
    grep -rIlE "$SECRET_PATTERN" "$TMP_DIR" >&2
    exit 1
fi

# Mirror-Arbeitsverzeichnis auf den gefilterten Stand bringen (voller Resync).
rsync -a --delete --exclude='.git' "$TMP_DIR"/ "$MIRROR_DIR"/

cd "$MIRROR_DIR"
git add -A
if git diff --cached --quiet; then
    echo "$LOG_PREFIX keine Änderungen, nichts zu committen."
    exit 0
fi

git commit -q -m "Sync von internem Repo @ ${COMMIT_SHA} (${COMMIT_DATE})"
git push origin main
echo "$LOG_PREFIX Mirror aktualisiert (Quelle: ${COMMIT_SHA})."
