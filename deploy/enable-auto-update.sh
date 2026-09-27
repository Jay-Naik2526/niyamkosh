#!/usr/bin/env bash
# Turn on auto-update from GitHub. setup.sh runs this; run it by hand on a
# server that was set up before auto-update existed:
#
#   cd ~/niyamkosh && git pull && bash deploy/enable-auto-update.sh
#
# Needs the repo cloned over SSH (git@github.com:...) with a read-only deploy
# key, so the server can fetch a private repo without a password.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
APP_USER="$(whoami)"

if ! git fetch --quiet origin main; then
  echo "This server cannot fetch from GitHub yet. Add its deploy key to the repo first." >&2
  exit 1
fi

for unit in niyamkosh-update.service niyamkosh-update.timer; do
  sed -e "s#APP_USER#$APP_USER#g" -e "s#APP_DIR#$APP_DIR#g" "deploy/$unit" \
    | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now niyamkosh-update.timer

echo "✔ Auto-update is on: every 5 minutes this server checks GitHub for new commits."
echo "  Last runs:   journalctl -u niyamkosh-update -n 20"
echo "  Next check:  systemctl list-timers niyamkosh-update.timer"
