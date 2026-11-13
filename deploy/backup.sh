#!/usr/bin/env bash
# Backup diário: bancos da Bella (cópia íntegra do SQLite), .env da Bella e a
# chave + banco da FreeLLMAPI. Guarda os 7 mais recentes em ~/backups-bella.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/backups-bella"
STAMP="$(date +%Y-%m-%d_%H%M)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$DEST" "$WORK/bella" "$WORK/freellmapi"

# SQLite em uso: .backup gera cópia íntegra mesmo com a Bella rodando.
cp -a "$APP_DIR/data" "$WORK/bella/data"
for db in "$APP_DIR"/data/*.db; do
  [ -f "$db" ] || continue
  sqlite3 "$db" ".backup '$WORK/bella/data/$(basename "$db")'"
done
cp "$APP_DIR/.env" "$WORK/bella/.env"

cp "$APP_DIR/deploy/freellmapi/.env" "$WORK/freellmapi/.env" 2>/dev/null || true
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx bella-freellmapi; then
  docker cp bella-freellmapi:/app/server/data "$WORK/freellmapi/data" >/dev/null 2>&1 || true
fi

tar -czf "$DEST/bella-$STAMP.tar.gz" -C "$WORK" .
chmod 600 "$DEST/bella-$STAMP.tar.gz"
ls -1t "$DEST"/bella-*.tar.gz | tail -n +8 | xargs -r rm -f
echo "$(date '+%F %T') backup ok: bella-$STAMP.tar.gz"
