#!/usr/bin/env bash
set -euo pipefail
sudo systemctl start deriv-analyzer.service
systemctl --no-pager --lines=0 status deriv-analyzer.service
