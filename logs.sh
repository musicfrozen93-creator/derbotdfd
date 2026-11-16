#!/usr/bin/env bash
# Follow the rotating application log.  ./logs.sh [N lines]   |  ./logs.sh journal
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == "journal" ]]; then
  exec journalctl -u deriv-analyzer.service -f
fi
exec tail -n "${1:-100}" -F "$DIR/logs/analyzer.log"
