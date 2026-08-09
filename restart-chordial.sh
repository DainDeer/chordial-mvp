#!/usr/bin/env bash
set -euo pipefail

SERVICE="chordial.service"

echo "== pulling latest main =="
cd ~/chordial-mvp
git pull

echo "== restarting $SERVICE =="
sudo systemctl restart "$SERVICE"

echo "== status =="
sudo systemctl status "$SERVICE" --no-pager -l

echo
echo "== tailing logs (ctrl-c to stop) =="
sudo journalctl -u "$SERVICE" -f --no-pager
