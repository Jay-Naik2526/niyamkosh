#!/usr/bin/env bash
# Run by niyamkosh-update.timer every few minutes. Does nothing unless GitHub
# has a new commit on main; then pulls it, reinstalls requirements only if they
# changed, restarts NiyamKosh and checks it answers. If the new version does
# not come up, it goes back to the previous commit — a bad push must not take
# the site down in front of an evaluator.
set -uo pipefail
cd "$(dirname "$0")/.."

log() { echo "[auto-update] $*"; }
healthy() {
  for _ in $(seq 1 60); do
    curl -fs http://127.0.0.1:8000/health >/dev/null && return 0
    sleep 2
  done
  return 1
}

git fetch --quiet origin main || { log "could not reach GitHub, will try again"; exit 0; }
old="$(git rev-parse HEAD)"
new="$(git rev-parse origin/main)"
[ "$old" = "$new" ] && exit 0
# A commit that already failed to start is not retried every five minutes —
# that would take the site down for two minutes in every five. The next push
# replaces it and is tried normally.
SKIP=.auto-update-failed
if [ -f "$SKIP" ] && [ "$(cat "$SKIP")" = "$new" ]; then exit 0; fi

changed="$(git diff --name-only "$old" "$new")"
log "updating ${old:0:7} -> ${new:0:7}"
if ! git merge --ff-only --quiet origin/main; then
  log "cannot fast-forward (local changes on the server?) — left as is"
  exit 1
fi

if grep -qx 'requirements.txt' <<<"$changed"; then
  log "requirements changed, installing"
  venv/bin/pip install --quiet -r requirements.txt
fi
if grep -qx 'deploy/niyamkosh.service' <<<"$changed"; then
  log "service file changed, reinstalling it"
  sed -e "s#APP_USER#$(whoami)#g" -e "s#APP_DIR#$(pwd)#g" deploy/niyamkosh.service \
    | sudo tee /etc/systemd/system/niyamkosh.service >/dev/null
  sudo systemctl daemon-reload
fi

sudo systemctl restart niyamkosh
if healthy; then
  log "running ${new:0:7}"
  exit 0
fi

log "new version did not start — rolling back to ${old:0:7}; ${new:0:7} will not be retried"
echo "$new" > "$SKIP"
git reset --hard --quiet "$old"
if grep -qx 'requirements.txt' <<<"$changed"; then
  venv/bin/pip install --quiet -r requirements.txt
fi
sudo systemctl restart niyamkosh
healthy && log "rolled back, running ${old:0:7}" || log "still not answering — check: journalctl -u niyamkosh -n 50"
exit 1
