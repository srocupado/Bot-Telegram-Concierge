#!/usr/bin/env bash
# Backup do SQLite do Concierge com retenção de 14 dias.
# Uso: ./scripts/backup.sh [BACKUP_DIR]
# Padrão BACKUP_DIR: /mnt/kodak/Bot-Concierge
# Recomendado rodar via cron diário (ver README).
#
# Usa a Online Backup API do SQLite (sqlite3.backup) — produz uma cópia
# transacionalmente consistente mesmo com o bot escrevendo no banco
# (necessário com journal_mode=WAL).

set -euo pipefail

BACKUP_DIR="${1:-/mnt/kodak/Bot-Concierge}"
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
TMP_NAME=".concierge-backup-tmp.db"

# Snapshot consistente via Online Backup API (roda no container; o
# módulo sqlite3 do Python já vem na imagem base).
docker compose -f "$REPO_DIR/docker-compose.yml" exec -T concierge python - <<PY
import sqlite3
src = sqlite3.connect("/app/data/concierge.db")
dst = sqlite3.connect("/app/data/${TMP_NAME}")
with dst:
    src.backup(dst)
src.close()
dst.close()
PY

# Empacota só o snapshot (não o .db ao vivo + sidecars).
tar czf "$OUT" -C "$DATA_DIR" "$TMP_NAME"
rm -f "$DATA_DIR/$TMP_NAME"

find "$BACKUP_DIR" -name 'concierge-*.tgz' -mtime "+$RETENTION_DAYS" -delete

echo "backup gerado: $OUT"
