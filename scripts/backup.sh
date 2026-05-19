#!/usr/bin/env bash
# Backup do SQLite do Concierge com retenção de 14 dias.
# Uso: ./scripts/backup.sh [BACKUP_DIR]
# Padrão BACKUP_DIR: /var/backups/concierge
# Recomendado rodar via cron diário (ver README).

set -euo pipefail

BACKUP_DIR="${1:-/var/backups/concierge}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$REPO_DIR/data"
RETENTION_DAYS=14

if [[ ! -d "$DATA_DIR" ]]; then
  echo "diretório de dados não existe: $DATA_DIR" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F-%H%M)"
OUT="$BACKUP_DIR/concierge-$STAMP.tgz"

tar czf "$OUT" -C "$REPO_DIR" data

find "$BACKUP_DIR" -name 'concierge-*.tgz' -mtime "+$RETENTION_DAYS" -delete

echo "backup gerado: $OUT"
