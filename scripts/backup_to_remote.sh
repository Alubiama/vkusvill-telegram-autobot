#!/bin/bash
# Daily offsite backup of state.db via Tailscale SFTP to two Windows hosts.
# Uses VACUUM INTO for a consistent single-file copy (no WAL artifacts).
set -euo pipefail

cd "$(dirname "$0")/.."

DATE=$(date +%Y-%m-%d)
TMP="/tmp/vkusvill-state-${DATE}.db"
LOG="data/backup_remote.log"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG"
}

cleanup() {
  rm -f "$TMP"
}
trap cleanup EXIT

log "VACUUM INTO $TMP starting"
.venv/bin/python -c "
import sqlite3, sys
src = sqlite3.connect('data/state.db')
src.execute('VACUUM INTO ?', (sys.argv[1],))
src.close()
" "$TMP"

SIZE=$(stat -c %s "$TMP")
log "VACUUM ok, size=${SIZE} bytes"

OK_HOSTS=()
FAIL_HOSTS=()
for HOST in mytishchi odintsovo; do
  if sftp -b - "$HOST" >/dev/null 2>&1 <<EOF
-mkdir vkusvill_backups
put $TMP vkusvill_backups/state-${DATE}.db
bye
EOF
  then
    OK_HOSTS+=("$HOST")
    log "uploaded -> $HOST"
  else
    FAIL_HOSTS+=("$HOST")
    log "FAIL -> $HOST"
  fi
done

PRUNED=$(find data/ -maxdepth 1 -name 'http_api_waves_*.json' -mtime +14 -print -delete | wc -l)
if [ "$PRUNED" -gt 0 ]; then
  log "pruned $PRUNED wave file(s) older than 14d"
fi

if [ ${#FAIL_HOSTS[@]} -gt 0 ]; then
  log "WARNING: ${#FAIL_HOSTS[@]} of 2 hosts failed: ${FAIL_HOSTS[*]}"
  exit 1
fi

log "all hosts ok: ${OK_HOSTS[*]}"
