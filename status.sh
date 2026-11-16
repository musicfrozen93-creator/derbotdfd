#!/usr/bin/env bash
# Service state + heartbeat + current evaluation summary.   Usage: sudo ./status.sh
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="${APP_USER:-deriv-analyzer}"
systemctl --no-pager --lines=0 status deriv-analyzer.service
echo
PY="$DIR/.venv/bin/python"; [[ -x "$PY" ]] || PY=python3
cd "$DIR" || exit 1
# The database belongs to the service user; read it as that user when run with sudo.
if [[ $EUID -eq 0 ]] && id -u "$APP_USER" >/dev/null 2>&1; then
  sudo -u "$APP_USER" "$PY" scripts/report.py --brief
else
  "$PY" scripts/report.py --brief
fi
