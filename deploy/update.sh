#!/usr/bin/env bash
# homeWatch updater. install.sh sets it up as a systemd unit (homewatch-update.path / .service) that runs this
# script when the System page leaves a request in update/ask/. The request is one of a few fixed words; anything
# else, or an unreadable request, means "update". The page can't choose a version, a file or a command:
#   update           read the latest release from GitHub, set HW_VERSION in .env to it, pull and restart the stack.
#                    When the download fails, or the new release doesn't become healthy within 3 minutes, .env is
#                    put back and the previous release started again.
#   grafana on|off   add or remove "grafana" in COMPOSE_PROFILES in .env and start or remove the Grafana container
#                    (its dashboards and data stay in their volumes)
#   retention 1y|180d|90d|30d   set HW_RETENTION in .env and restart VictoriaMetrics; data older than that is dropped
# and writes how it went to update/status, which the System page shows.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

status() {  # update/ is root's; homewatch can only write in update/ask/
  printf '%s %s %s\n' "$(date +%s)" "$1" "$2" > update/status.tmp
  mv -f update/status.tmp update/status
}

# what this updater can do, for the System page (older updaters don't write it, so the page offers only Update)
echo "update grafana retention" > update/features.tmp && mv -f update/features.tmp update/features

[[ -e update/ask/request || -L update/ask/request ]] || exit 0
req=""
if [[ -f update/ask/request && ! -L update/ask/request ]]; then
  req=$(head -c 40 update/ask/request | head -n 1 | tr -cd 'a-z0-9 ')  # only compared with the fixed words below
fi
rm -f update/ask/request

# installs from before the port settings published 443 and 80: a newer docker-compose.yml must keep them
grep -q '^HW_HTTPS_PORT=' .env || echo "HW_HTTPS_PORT=443" >> .env
grep -q '^HW_HTTP_PORT=' .env || echo "HW_HTTP_PORT=80" >> .env

dc() {  # docker compose with this install's files: COMPOSE_FILE in .env if set (e.g. a development build), else the release's
  if grep -q '^COMPOSE_FILE=.' .env; then docker compose "$@"; else docker compose -f docker-compose.yml "$@"; fi
}
setenv() {  # setenv NAME VALUE: set or add one line in .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
case "$req" in
  "grafana on"|"grafana off")
    cp -p .env .env.before-grafana
    profiles=$(sed -n 's/^COMPOSE_PROFILES=//p' .env | head -1 | tr ',' '\n' | grep -v '^grafana$' | grep -v '^$' | paste -sd, - || true)
    if [[ "$req" == "grafana on" ]]; then
      setenv COMPOSE_PROFILES "${profiles:+$profiles,}grafana"
      status running "turning Grafana on"
      if dc pull grafana && dc up -d; then
        status ok "Grafana turned on"
      else
        cp -p .env.before-grafana .env
        status failed "turning Grafana on failed (see journalctl -u homewatch-update)"
      fi
    else
      setenv COMPOSE_PROFILES "$profiles"
      dc --profile grafana rm -sf grafana >/dev/null 2>&1 || true
      status ok "Grafana turned off (its dashboards are kept)"
    fi
    exit 0 ;;
  "retention 1y"|"retention 180d"|"retention 90d"|"retention 30d")
    keep=${req#retention }
    cp -p .env .env.before-retention
    setenv HW_RETENTION "$keep"
    status running "keeping $keep of history"
    if dc up -d victoriametrics controller; then
      status ok "history kept for $keep"
    else
      cp -p .env.before-retention .env
      dc up -d || true
      status failed "changing the history length failed (see journalctl -u homewatch-update)"
    fi
    exit 0 ;;
esac

# read the whole answer first: a pipe that stops early (grep -m1) makes curl fail under pipefail
json=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/nikosiaf/homewatch/releases/latest) || json=""
latest=$(sed -nE 's/.*"tag_name": *"v?([^"]*)".*/\1/p' <<<"$json")
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
healthy() {  # the controller's own health check says it is up, within 3 minutes
  local id st
  for _ in $(seq 1 36); do
    id=$(docker compose -f docker-compose.yml ps -q controller 2>/dev/null || true)
    st=$(docker inspect --format '{{.State.Health.Status}}' "$id" 2>/dev/null || true)
    [[ "$st" == "healthy" ]] && return 0
    sleep 5
  done
  return 1
}
back() {  # put the previous release back
  cp -p .env.before-update .env
  docker compose -f docker-compose.yml up -d || true
  status failed "installing $latest failed, still on ${current:-?}: $1 (see journalctl -u homewatch-update)"
  exit 1
}
docker compose -f docker-compose.yml pull || back "the download failed"
docker compose -f docker-compose.yml up -d || back "it didn't start"
healthy || back "it didn't become healthy within 3 minutes, so the previous release is back"
# Grafana reads its data source only when it starts; the new homeWatch may have changed it
docker compose -f docker-compose.yml restart grafana >/dev/null 2>&1 || true
status ok "$latest installed (was ${current:-?})"
