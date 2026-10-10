#!/usr/bin/env bash
# Back up the Tesla CLI state directory (config, tokens, private key).
# Run on the server, from anywhere; paths can be overridden via env.
set -euo pipefail

ROOT="${TESLA_DIR:-$HOME/tesla}"
BACKUP_DIR="${TESLA_BACKUP_DIR:-$ROOT/backups}"
KEEP="${TESLA_BACKUP_KEEP:-8}"

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
archive="$BACKUP_DIR/tesla-config-$stamp.tgz"

tar -czf "$archive" -C "$ROOT" config
chmod 600 "$archive"

# Keep the newest $KEEP archives.
ls -1t "$BACKUP_DIR"/tesla-config-*.tgz \
  | tail -n +$((KEEP + 1)) \
  | xargs -r rm -f

echo "wrote $archive"
