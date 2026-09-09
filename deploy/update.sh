#!/usr/bin/env bash
#
# Bring the server up to the newest version.  sudo drishti-update
#
# This replaces downloading a ZIP and extracting it over a folder. The
# database lives outside the code folder, so updating cannot touch it.

set -euo pipefail

APP_DIR="${DRISHTI_APP_DIR:-/opt/drishti}"
DATA_DIR="${DRISHTI_DATA_DIR:-/var/lib/drishti}"
BACKUP_DIR="$DATA_DIR/backups"
ENV_FILE="${DRISHTI_ENV_FILE:-/etc/drishti/drishti.env}"
SERVICE_USER="${DRISHTI_USER:-drishti}"
SERVICE="${DRISHTI_SERVICE:-drishti}"
KEEP_BACKUPS=10

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo: sudo drishti-update" >&2
  exit 1
fi
if [ ! -d "$APP_DIR/.git" ]; then
  echo "No Drishti install at $APP_DIR. Run deploy/server-setup.sh first." >&2
  exit 1
fi

BRANCH="${DRISHTI_BRANCH:-$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)}"
was="$(git -C "$APP_DIR" rev-parse --short HEAD)"

git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
target="$(git -C "$APP_DIR" rev-parse --short "origin/$BRANCH")"

# Decided before anything is touched. Backing up on every run would fill
# the folder with identical copies and push out the one taken before the
# update that actually changed something.
if [ "$was" = "$target" ]; then
  echo "Already the newest version ($was). Nothing to do."
  exit 0
fi

echo "Updating $was -> $target"

# A new version can bring database migrations, and those run the moment
# the app starts. Copy it first, while the version that wrote it is still
# the one running. sqlite3's own backup rather than cp, so the copy holds
# together even though the app has the file open.
DB="$(sed -n 's/^SUCHAK_DB=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
if [ -n "${DB:-}" ] && [ -f "$DB" ]; then
  mkdir -p "$BACKUP_DIR"
  backup="$BACKUP_DIR/suchak-$(date +%Y%m%d-%H%M%S)-before-$target.db"
  "$APP_DIR/.venv/bin/python" - "$DB" "$backup" <<'PYBK'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close()
s.close()
PYBK
  chown -R "$SERVICE_USER:$SERVICE_USER" "$BACKUP_DIR"
  echo "Database backed up to $backup"
  ls -1t "$BACKUP_DIR"/suchak-*.db 2>/dev/null \
    | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm --
else
  echo "No database found at '${DB:-unset}' -- nothing to back up."
fi

# Recorded before the switch, so drishti-rollback knows where back is.
echo "$was" > "$DATA_DIR/previous-version"

git -C "$APP_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

"$APP_DIR/.venv/bin/pip" install --quiet --upgrade -r "$APP_DIR/requirements.txt"
# The service runs with the code folder read-only, so it cannot write its
# own bytecode cache. Write it here, as root, or it recompiles every start.
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/app" >/dev/null || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.venv"

systemctl restart "$SERVICE"
sleep 3

if ! systemctl is-active --quiet "$SERVICE"; then
  echo >&2
  echo "Drishti did not come back up. What it said as it stopped:" >&2
  journalctl -u "$SERVICE" -n 25 --no-pager >&2
  echo >&2
  echo "To go back to the version that was working:" >&2
  echo "  sudo git -C $APP_DIR checkout $was && sudo systemctl restart $SERVICE" >&2
  echo "The database as it stood before this update is in $BACKUP_DIR." >&2
  exit 1
fi

echo "Updated $was -> $target, and Drishti is running."
echo "Check the build number in the page footer to confirm the browser sees it."
echo "If this version turns out to be wrong:  sudo drishti-rollback"
