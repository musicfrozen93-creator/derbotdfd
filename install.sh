#!/usr/bin/env bash
# Deriv Market Condition Analyzer — installer for Ubuntu 22.04 / 24.04 LTS.
# Usage (from the unpacked project folder):   sudo ./install.sh
set -euo pipefail

APP_NAME="deriv-analyzer"
APP_DIR="${APP_DIR:-/opt/deriv-market-analyzer}"
APP_USER="${APP_USER:-deriv-analyzer}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo ./install.sh" >&2
  exit 1
fi

echo "==> 1/8 Installing Ubuntu packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates tzdata sqlite3 rsync software-properties-common \
  python3 python3-venv python3-pip

pick_python() {
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      echo "$c"; return 0
    fi
  done
  return 1
}

PY="$(pick_python || true)"
if [[ -z "$PY" ]]; then
  echo "==> Python 3.11+ not found; installing python3.11"
  if ! apt-get install -y python3.11 python3.11-venv; then
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -y
    apt-get install -y python3.11 python3.11-venv
  fi
  PY="python3.11"
fi
"$PY" -m venv --help >/dev/null 2>&1 || apt-get install -y "$(basename "$PY")-venv" || true
echo "    using $($PY --version)"

echo "==> 2/8 Creating service user and application directory"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR"
KEEP_CONFIG=()
if [[ -f "$APP_DIR/config.yaml" ]] && ! cmp -s "$SRC_DIR/config.yaml" "$APP_DIR/config.yaml"; then
  # an upgrade must not overwrite settings edited on the server
  KEEP_CONFIG=(--exclude '/config.yaml')
  cp "$SRC_DIR/config.yaml" "$APP_DIR/config.yaml.new"
  echo "    kept your $APP_DIR/config.yaml (the shipped version is saved as config.yaml.new)"
fi
rsync -a --delete \
  --exclude '/.venv' --exclude '/data' --exclude '/logs' --exclude '/.env' --exclude '__pycache__' \
  --exclude '.pytest_cache' --exclude '/.git' --exclude '/config.yaml.new' "${KEEP_CONFIG[@]}" \
  "$SRC_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

echo "==> 3/8 Creating Python virtual environment"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$PY" -m venv "$APP_DIR/.venv"
fi

echo "==> 4/8 Installing Python dependencies"
"$APP_DIR/.venv/bin/pip" install --upgrade pip wheel >/dev/null
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
# the service runs with a read-only code directory, so compile the bytecode now
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/app" "$APP_DIR/scripts" >/dev/null

echo "==> 5/8 Preparing .env (secrets)"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "    created $APP_DIR/.env — add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
fi
# Code belongs to root (the service cannot change it); only data/ and logs/ belong to the
# service user. .env is readable by root and the service group only.
chown -R root:root "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data" "$APP_DIR/logs"
chmod 750 "$APP_DIR/data"
chown root:"$APP_USER" "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"
chmod +x "$APP_DIR"/*.sh || true

echo "==> 6/8 Initialising SQLite database (WAL mode)"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/init_db.py"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m app.main --validate >/dev/null
echo "    strategies validated"

echo "==> 7/8 Installing systemd service"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__APP_USER__#$APP_USER#g" \
  "$APP_DIR/systemd/deriv-analyzer.service" > "/etc/systemd/system/${APP_NAME}.service"
systemctl daemon-reload
systemctl enable "${APP_NAME}.service"

echo "==> 8/8 Starting the service"
systemctl restart "${APP_NAME}.service"
sleep 3
systemctl --no-pager --lines=5 status "${APP_NAME}.service" || true

cat <<EOF

Installed to $APP_DIR and enabled at boot (service: ${APP_NAME}).

Next steps
  1. sudo nano $APP_DIR/.env        # set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
  2. sudo $APP_DIR/restart.sh
  3. sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/scripts/test_telegram.py
     # sends a TEST START and a TEST STOP; real signal states are not touched
  4. sudo $APP_DIR/status.sh   /   $APP_DIR/logs.sh

The first start downloads history (bootstrap) for every compatible market; this
takes roughly an hour at the configured rate limit. Until enough data and a
payout quote exist, every combination stays INSUFFICIENT_DATA and no START is sent.
EOF
