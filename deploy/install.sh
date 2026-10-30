#!/usr/bin/env bash
# homewatch installer. Please read it before running it; it is short and only does this:
#   1. creates .env from .env.example with a new master key, this host's time zone and
#      its gateway / DNS server as defaults for the setup wizard (skipped if .env exists)
#   2. creates a self-signed HTTPS certificate in ./certs (skipped if one exists)
#   3. pulls the images and starts the stack with docker compose
# Usage: ./install.sh          (release images from ghcr.io)
#        ./install.sh --dev    (build the controller from ../controller)
set -euo pipefail
cd "$(dirname "$0")"

compose=(docker compose -f docker-compose.yml)
if [[ "${1:-}" == "--dev" ]]; then compose+=(-f docker-compose.dev.yml); fi

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

if [[ "${1:-}" == "--dev" ]]; then
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
code=""
for _ in $(seq 1 30); do
  code=$("${compose[@]}" logs controller 2>/dev/null | grep -o 'First-run setup code: .*' | tail -1 | cut -d' ' -f4)
  [[ -n "$code" ]] && break
  sleep 2
done
if [[ -n "$code" ]]; then
  echo "The setup wizard asks for this one-time code: $code"
else
  echo "If the setup wizard asks for a code: ${compose[*]} logs controller | grep \"setup code\""
fi
