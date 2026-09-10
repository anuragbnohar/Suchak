#!/usr/bin/env bash
#
# Put back the version that was running before the last update.
#
#   sudo drishti-rollback
#
# Safe to do. Every database change this app has ever made adds a column;
# nothing is dropped or rewritten. An older version simply ignores the
# columns it does not know about, so the data stays whole either way.
#
# To come forward again afterwards, run drishti-update.

set -euo pipefail

APP_DIR="${DRISHTI_APP_DIR:-/opt/drishti}"
DATA_DIR="${DRISHTI_DATA_DIR:-/var/lib/drishti}"
SERVICE_USER="${DRISHTI_USER:-drishti}"
SERVICE="${DRISHTI_SERVICE:-drishti}"
PREV_FILE="$DATA_DIR/previous-version"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo: sudo drishti-rollback" >&2
  exit 1
fi
if [ ! -d "$APP_DIR/.git" ]; then
  echo "No Drishti install at $APP_DIR." >&2
  exit 1
fi
if [ ! -s "$PREV_FILE" ]; then
  echo "No previous version recorded -- nothing has been updated yet." >&2
  echo "Nothing to roll back to." >&2
  exit 1
fi

prev="$(tr -d '[:space:]' < "$PREV_FILE")"
now="$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" rev-parse --short HEAD)"

if [ "$prev" = "$now" ]; then
  echo "Already running $now, which is the version rollback would put back."
  exit 0
fi
if ! git -c safe.directory="$APP_DIR" -C "$APP_DIR" cat-file -e "$prev^{commit}" 2>/dev/null; then
  echo "The recorded version $prev is not in the repository any more." >&2
  exit 1
fi

echo "Rolling back $now -> $prev"

# reset rather than checkout: it keeps the branch name pointing at the
# older commit, so drishti-update can still tell what to compare against
# and will bring this forward again when asked.
git -c safe.directory="$APP_DIR" -C "$APP_DIR" reset --quiet --hard "$prev"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# The older version may want different packages than the newer one did.
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/app" >/dev/null || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.venv"

# Rolling back makes the version we just left the one to come back to.
echo "$now" > "$PREV_FILE"

systemctl restart "$SERVICE"
sleep 3

if ! systemctl is-active --quiet "$SERVICE"; then
  echo >&2
  echo "Drishti did not come back up on $prev either. What it said:" >&2
  journalctl -u "$SERVICE" -n 25 --no-pager >&2
  echo >&2
  echo "Database copies from before each update are in $DATA_DIR/backups." >&2
  exit 1
fi

echo "Rolled back to $prev, and Drishti is running."
echo "Check the build number in the page footer."
echo
echo "To come forward to the newest version:  sudo drishti-update"
echo "Running drishti-rollback again returns to $now -- it is an undo of"
echo "this undo, not a second step further back."
