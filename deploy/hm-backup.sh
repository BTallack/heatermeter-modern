#!/usr/bin/env bash
#
# Nightly local backup of the HeaterMeter database + configuration.
#
# Snapshots the SQLite history via the online-backup API (safe while the daemon
# is writing) and tars the JSON config files, into <repo>/backups/, keeping the
# newest 14 of each. Runs as the daemon user from hm-backup.timer.
#
# These backups protect against corruption and accidental deletion. They live
# on the same SD card, so for disaster protection point an offsite copy at the
# backups dir, e.g. a cron on another machine:
#   rsync -az pi:<repo>/backups/ /your/nas/heatermeter-backups/
set -euo pipefail

REPO="${HM_BACKUP_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BK="$REPO/backups"
DB="$REPO/data/hm.sqlite"
KEEP=14
STAMP="$(date +%Y%m%d-%H%M)"

mkdir -p "$BK"

# Consistent DB snapshot (SQLite online backup), then compress.
if [ -f "$DB" ]; then
  "$REPO/.venv/bin/python" - "$DB" "$BK/hm-$STAMP.sqlite" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
  gzip -f "$BK/hm-$STAMP.sqlite"
fi

# All the small JSON configs (mqtt, notify, cookdone, profiles, auth, ...).
if compgen -G "$REPO/data/*.json" > /dev/null; then
  tar -czf "$BK/config-$STAMP.tar.gz" -C "$REPO/data" \
    $(cd "$REPO/data" && ls *.json)
fi

# Rotate: keep the newest $KEEP of each kind.
for pat in 'hm-*.sqlite.gz' 'config-*.tar.gz'; do
  ls -1t "$BK"/$pat 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
done

echo "backup ok: $(ls -1 "$BK" | wc -l) files in $BK"
