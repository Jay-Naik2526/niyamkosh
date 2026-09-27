#!/usr/bin/env bash
# One-time server setup for NiyamKosh on a fresh Ubuntu 24.04 VM.
#
#   cd ~/niyamkosh && bash deploy/setup.sh
#
# It asks for four things — the site address, a username, a password, and the
# API keys — and does the rest: packages, swap, Python environment, models,
# the system service and HTTPS. Safe to run again; each step checks first.
set -euo pipefail
# Ubuntu 24.04 otherwise stops mid-install on a "restart services?" dialog.
export NEEDRESTART_MODE=a

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="$(whoami)"
say() { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }

if [ "$APP_USER" = "root" ]; then
  echo "Run this as your normal user (e.g. azureuser), not with sudo." >&2
  exit 1
fi

# ── questions first, so the long part runs unattended ─────────────────────
read -rp "Site address (e.g. niyamkosh.centralindia.cloudapp.azure.com): " DOMAIN
read -rp "Login username for the site [ragnarok]: " SITE_USER
SITE_USER="${SITE_USER:-ragnarok}"
while true; do
  read -rsp "Login password for the site: " PASS1; echo
  read -rsp "Same password again: " PASS2; echo
  [ -n "$PASS1" ] && [ "$PASS1" = "$PASS2" ] && break
  echo "Passwords were empty or did not match — try again."
done
if [ ! -f "$APP_DIR/.env" ]; then
  read -rsp "Bhashini inference key (Enter to skip): " BHASHINI; echo
  read -rsp "Gemini API key (Enter to skip): " GEMINI; echo
fi

say "Updating Ubuntu and installing tools"
sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get upgrade -y
sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y \
  python3-venv python3-pip git curl debian-keyring debian-archive-keyring apt-transport-https

say "Adding 2 GB of swap (a safety net for the install on a 2 GB machine)"
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
# Use swap only when memory is really short; keep the app in RAM otherwise.
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-niyamkosh.conf >/dev/null
sudo sysctl -q -p /etc/sysctl.d/99-niyamkosh.conf

say "Creating the Python environment (CPU-only torch — no GPU, a fraction of the size)"
cd "$APP_DIR"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
venv/bin/pip install --quiet -r requirements.txt

say "Downloading the two AI models now, so the first visitor does not wait"
OMP_NUM_THREADS=1 venv/bin/python warm_models.py

if [ ! -f .env ]; then
  say "Writing .env (API keys — stays on this server, never in git)"
  (
    umask 077   # in a subshell: a global umask would make Caddy's config unreadable to Caddy
    {
      [ -n "${BHASHINI:-}" ] && echo "BHASHINI_INFERENCE_KEY=$BHASHINI"
      [ -n "${GEMINI:-}" ] && echo "GEMINI_API_KEY=$GEMINI"
      true
    } > .env
  )
fi
chmod 600 .env

say "Installing the NiyamKosh service"
sed -e "s#APP_USER#$APP_USER#g" -e "s#APP_DIR#$APP_DIR#g" deploy/niyamkosh.service \
  | sudo tee /etc/systemd/system/niyamkosh.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now niyamkosh
sudo systemctl restart niyamkosh

say "Installing Caddy (HTTPS + password)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y
  sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y caddy
fi
HASH="$(caddy hash-password --plaintext "$PASS1")"
sed -e "s#DOMAIN#$DOMAIN#g" -e "s#SITE_USER#$SITE_USER#g" -e "s#PASSWORD_HASH#$HASH#g" \
  deploy/Caddyfile.template | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload caddy || sudo systemctl restart caddy

say "Waiting for NiyamKosh to answer"
for _ in $(seq 1 60); do
  if curl -fs http://127.0.0.1:8000/health >/dev/null; then
    printf '\n\033[1;32m✔ Done.\033[0m  Open https://%s  and log in as %s\n\n' "$DOMAIN" "$SITE_USER"
    exit 0
  fi
  sleep 2
done
echo "The app did not answer within two minutes. See why with:"
echo "  sudo journalctl -u niyamkosh -n 50"
exit 1
