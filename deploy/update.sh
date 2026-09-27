#!/usr/bin/env bash
# Pull the latest code from GitHub and restart NiyamKosh.
#
#   cd ~/niyamkosh && bash deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
venv/bin/pip install --quiet -r requirements.txt
sudo systemctl restart niyamkosh
for _ in $(seq 1 60); do
  curl -fs http://127.0.0.1:8000/health >/dev/null && { echo "✔ Updated and running"; exit 0; }
  sleep 2
done
echo "Not answering yet — check: sudo journalctl -u niyamkosh -n 50"
exit 1
