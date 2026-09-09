#!/usr/bin/env bash
#
# Put Drishti on an always-on Ubuntu server, in one command.
#
#   sudo bash server-setup.sh
#
# Safe to run again: it updates an existing install rather than replacing
# it, and it never overwrites the secrets file or the database.
#
# It does NOT open a port. Nothing reaches this machine from the internet
# directly -- Cloudflare Tunnel dials out from here, and the tunnel is
# installed separately with the command Cloudflare gives you.

set -euo pipefail

REPO="${DRISHTI_REPO:-https://github.com/anuragbnohar/Suchak.git}"
BRANCH="${DRISHTI_BRANCH:-claude/file-review-suggestions-9nqs4a}"
APP_DIR="${DRISHTI_APP_DIR:-/opt/drishti}"
DATA_DIR="${DRISHTI_DATA_DIR:-/var/lib/drishti}"
ENV_DIR="${DRISHTI_ENV_DIR:-/etc/drishti}"
ENV_FILE="$ENV_DIR/drishti.env"
SERVICE_USER="${DRISHTI_USER:-drishti}"
PORT="${DRISHTI_PORT:-8000}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo: sudo bash server-setup.sh" >&2
  exit 1
fi

say "Installing what Python needs"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates

say "Creating the drishti account and its folders"
# A login of its own, with no shell and no password. If the web app is
# ever broken into, the intruder lands as a user who owns one folder.
id -u "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 \
  "$APP_DIR" "$DATA_DIR" "$DATA_DIR/backups"
install -d -o root -g "$SERVICE_USER" -m 750 "$ENV_DIR"

say "Fetching the code ($BRANCH)"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" remote set-url origin "$REPO"
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
else
  git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

say "Installing the Python packages"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
# Compile now, as root. The service runs with the code folder read-only,
# so it cannot write its own bytecode cache; without this it recompiles
# every source file on every start.
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/app" >/dev/null || true
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.venv"

# The secrets file is written once. Re-running this script must never
# cost somebody the keys they typed in, so an existing file is left alone.
if [ ! -f "$ENV_FILE" ]; then
  say "Setting up the secrets file"
  secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

  anthropic_key="${ANTHROPIC_API_KEY:-}"
  if [ -z "$anthropic_key" ] && [ -t 0 ]; then
    echo
    echo "Paste your Anthropic API key and press Enter."
    echo "It is not shown as you type, and is stored only on this machine."
    echo "Leave it empty to add it later -- classification will not run until you do."
    read -rsp "Anthropic API key: " anthropic_key || true
    echo
  fi

  umask 077
  cat > "$ENV_FILE" <<ENV
# Drishti settings. Restart after editing:  sudo systemctl restart drishti
#
# Public mode is not cosmetic. It makes the app refuse to start without a
# real secret, or while any password printed in the README still works.
SUCHAK_PUBLIC=1
SUCHAK_SECRET=$secret
SUCHAK_DB=$DATA_DIR/suchak.db
PORT=$PORT

ANTHROPIC_API_KEY=$anthropic_key

# Optional. Fill in and restart to switch these sources on.
#SUCHAK_YOUTUBE_KEY=
#SUCHAK_X_BEARER=
#SUCHAK_X_ENABLED=1

# Fetching is manual by default: items arrive when somebody presses Fetch.
# A number here fetches every entity on a timer instead -- which bills the
# Anthropic account with nobody watching. Leave it off unless you mean it.
#SUCHAK_FETCH_MINUTES=360
ENV
  chown root:"$SERVICE_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
else
  say "Keeping the secrets file already at $ENV_FILE"
fi

say "Registering Drishti as a service"
cat > /etc/systemd/system/drishti.service <<UNIT
[Unit]
Description=Drishti
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python run.py
Restart=always
RestartSec=5

# The app writes exactly one thing: its database. Everything else on the
# machine is read-only to it.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT

install -m 755 "$APP_DIR/deploy/update.sh" /usr/local/bin/drishti-update

systemctl daemon-reload
systemctl enable --quiet drishti
systemctl restart drishti

sleep 3
if ! systemctl is-active --quiet drishti; then
  echo
  echo "Drishti did not stay running. What it said as it stopped:" >&2
  journalctl -u drishti -n 25 --no-pager >&2
  exit 1
fi

cat <<DONE

  Drishti is running on this server, on port $PORT, and will start
  itself again after a reboot or a crash.

  It is not reachable from the internet yet. Next:

    1. Install the tunnel, using the command Cloudflare gave you:
         (Zero Trust -> Networks -> Tunnels -> your tunnel -> Debian/Ubuntu)
    2. Point the tunnel's public hostname at  http://localhost:$PORT
    3. Copy your laptop's suchak.db up, if you have reviews worth keeping
         (see HOSTING.md -- "Bringing your existing data across")
    4. Make your first account:
         sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m app.newuser

  Useful later:
    sudo systemctl status drishti     is it running?
    sudo journalctl -u drishti -f     what is it saying?
    sudo drishti-update               fetch the newest version
    sudoedit $ENV_FILE                add a key, then restart

DONE
