#!/usr/bin/env bash
# homewatch updater. install.sh sets it up as a systemd unit (homewatch-update.path / .service) that runs this
# script when the System page's "Install" button leaves a request in update/ask/. It:
#   1. removes the request (its contents are never read: the page can't choose a version)
#   2. reads the latest release from GitHub
#   3. sets HW_VERSION in .env to it, pulls the images and restarts the stack
# and writes how it went to update/status, which the System page shows. On a failure .env is put back.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

status() {  # update/ is root's; homewatch can only write in update/ask/
  printf '%s %s %s\n' "$(date +%s)" "$1" "$2" > update/status.tmp
  mv -f update/status.tmp update/status
}

[[ -e update/ask/request || -L update/ask/request ]] || exit 0
rm -f update/ask/request

latest=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/nikosiaf/homewatch/releases/latest \
  | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"v?([^"]*)".*/\1/') || latest=""
if [[ ! "$latest" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  status failed "can't read the latest release from GitHub"
  exit 1
fi
current=$(sed -n 's/^HW_VERSION=//p' .env | head -1)
if [[ "$current" == "$latest" ]]; then
  status ok "$latest is already installed"
  exit 0
fi

status running "installing $latest (was ${current:-?})"
cp -p .env .env.before-update
if grep -q '^HW_VERSION=' .env; then
  sed -i "s/^HW_VERSION=.*/HW_VERSION=${latest}/" .env
else
  echo "HW_VERSION=${latest}" >> .env
fi
if docker compose -f docker-compose.yml pull && docker compose -f docker-compose.yml up -d; then
  status ok "$latest installed (was ${current:-?})"
else
  cp -p .env.before-update .env
  docker compose -f docker-compose.yml up -d || true
  status failed "installing $latest failed, still on ${current:-?} (see journalctl -u homewatch-update)"
  exit 1
fi
