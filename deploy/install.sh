#!/usr/bin/env bash
# homewatch installer. Please read it before running it; it is short and only does this:
#   1. creates .env from .env.example with a new master key, this host's time zone and
#      its gateway / DNS server as defaults for the setup wizard (skipped if .env exists)
#   2. creates a self-signed HTTPS certificate in ./certs (skipped if one exists)
#   3. offers the updater: a systemd unit that runs update.sh when the System page's "Update" button asks
#      (needs sudo once; homewatch itself gets no Docker access, it only leaves a file in update/ask/)
#   4. asks whether to add Grafana (COMPOSE_PROFILES=grafana in .env; System → Grafana can change it later)
#   5. pulls the images and starts the stack with docker compose
# Usage: ./install.sh               (release images from ghcr.io)
#        ./install.sh --no-updater  (the same, without the updater)
#        ./install.sh --grafana     (add Grafana without asking; --no-grafana: don't add it, don't ask)
#        ./install.sh --dev         (build the controller from ../controller; no updater)
set -euo pipefail
dev=0 updater=1 grafana=ask
for arg in "$@"; do
  case "$arg" in
    --dev) dev=1; updater=0 ;;
    --no-updater) updater=0 ;;
    --grafana) grafana=yes ;;
    --no-grafana) grafana=no ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done
cd "$(dirname "$0")"

compose=(docker compose -f docker-compose.yml)
if [[ $dev == 1 ]]; then compose+=(-f docker-compose.dev.yml); fi

for cmd in docker openssl ip; do
  command -v "$cmd" >/dev/null || { echo "Missing: $cmd" >&2; exit 1; }
done
docker compose version >/dev/null || { echo "Missing: the docker compose plugin" >&2; exit 1; }

ipv4() { grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | grep -vE '^127\.' | head -1; }

if [[ ! -f .env ]]; then
  echo "Creating .env"
  # release downloads name it env.example: GitHub drops the leading dot of attached files
  example=.env.example
  [[ -f "$example" ]] || example=env.example
  [[ -f "$example" ]] || { echo "Missing .env.example (or env.example) next to install.sh" >&2; exit 1; }
  install -m 600 "$example" .env
  key=$(openssl rand -base64 32)
  tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)
  gw=$(ip -4 route show default | awk '{print $3; exit}')
  dns=$( (resolvectl dns 2>/dev/null || true) | ipv4 || true)
  [[ -z "$dns" ]] && dns=$(grep -E '^nameserver' /etc/resolv.conf 2>/dev/null | ipv4 || true)
  sed -i -e "s|^HW_MASTER_KEY=.*|HW_MASTER_KEY=${key}|" -e "s|^TZ=.*|TZ=${tz}|" \
         -e "s|^HW_GATEWAY_HINT=.*|HW_GATEWAY_HINT=${gw}|" -e "s|^HW_DNS_HINT=.*|HW_DNS_HINT=${dns}|" .env
  echo "  time zone ${tz}, gateway ${gw:-?}, DNS ${dns:-?}"
else
  echo ".env exists, keeping it"
fi

host_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oE 'src ([0-9.]+)' | awk '{print $2}')
if [[ ! -f certs/cert.pem ]]; then
  echo "Creating a self-signed certificate"
  mkdir -p certs
  san="DNS:$(hostname),DNS:$(hostname).local"
  [[ -n "$host_ip" ]] && san="${san},IP:${host_ip}"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
    -keyout certs/key.pem -out certs/cert.pem -subj "/CN=homewatch" -addext "subjectAltName=${san}" 2>/dev/null
  chmod 600 certs/key.pem
fi

# The updater. update/ is root's (status), update/ask/ only homewatch's (uid 10001) for the request.
add_updater() {
  local dir sudo=()
  dir=$(pwd -P)
  [[ $EUID -eq 0 ]] || sudo=(sudo)
  [[ -x update.sh ]] || { echo "  update.sh is missing next to install.sh; skipping the updater"; return 0; }
  command -v systemctl >/dev/null && command -v curl >/dev/null \
    || { echo "  needs systemd and curl; skipping the updater"; return 0; }
  "${sudo[@]}" install -d -o root -g root -m 755 update
  "${sudo[@]}" install -d -o 10001 -m 700 update/ask
  echo "update grafana retention" | "${sudo[@]}" tee update/features >/dev/null  # update.sh rewrites it each run
  "${sudo[@]}" tee /etc/systemd/system/homewatch-update.service >/dev/null <<UNIT
[Unit]
Description=homewatch: install the latest release (asked from the System page)
After=docker.service

[Service]
Type=oneshot
ExecStart="${dir}/update.sh"
UNIT
  "${sudo[@]}" tee /etc/systemd/system/homewatch-update.path >/dev/null <<UNIT
[Unit]
Description=homewatch: wait for an update request from the System page

[Path]
PathExists=${dir}/update/ask/request

[Install]
WantedBy=multi-user.target
UNIT
  "${sudo[@]}" systemctl daemon-reload
  "${sudo[@]}" systemctl enable --now homewatch-update.path >/dev/null
  echo "  updater installed (homewatch-update.path); remove it with: sudo systemctl disable --now homewatch-update.path"
}
if [[ $updater == 1 ]]; then
  answer=y
  if [[ -t 0 ]]; then
    read -r -p "Add the updater, so the System page can install new releases? It needs sudo once. [Y/n] " answer
  fi
  if [[ ! "$answer" =~ ^[Nn] ]]; then
    echo "Adding the updater"
    add_updater
  fi
fi
mkdir -p update  # the controller mounts it; without the updater it stays empty

# Grafana: optional dashboards at https://<homewatch>/grafana/, signed in with the homewatch login
if [[ $grafana == ask ]] && ! grep -qE '^COMPOSE_PROFILES=.*grafana' .env; then
  grafana=no
  if [[ -t 0 ]]; then
    read -r -p "Add Grafana dashboards (about 2 GB download, 150 MB memory)? You can change it later on the System page. [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]] && grafana=yes
  fi
fi
if [[ $grafana == yes ]] && ! grep -qE '^COMPOSE_PROFILES=.*grafana' .env; then
  echo "Adding Grafana"
  if grep -q '^COMPOSE_PROFILES=.' .env; then sed -i 's/^COMPOSE_PROFILES=\(.*\)/COMPOSE_PROFILES=\1,grafana/' .env
  elif grep -q '^COMPOSE_PROFILES=' .env; then sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=grafana/' .env
  else echo "COMPOSE_PROFILES=grafana" >> .env; fi
fi

if [[ $dev == 1 ]]; then
  "${compose[@]}" up -d --build
else
  "${compose[@]}" pull
  "${compose[@]}" up -d
fi

echo
echo "homewatch is starting. Open https://${host_ip:-<this host>} and follow the setup wizard."
echo "Your browser will warn about the certificate once. Its SHA-256 fingerprint is:"
openssl x509 -in certs/cert.pem -noout -fingerprint -sha256 | cut -d= -f2
echo
echo "Create the admin account now: until it exists, whoever opens the page first creates it."
