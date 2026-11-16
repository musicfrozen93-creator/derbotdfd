#!/usr/bin/env bash
set -euo pipefail
sudo systemctl restart deriv-analyzer.service
sleep 2
systemctl --no-pager --lines=0 status deriv-analyzer.service
