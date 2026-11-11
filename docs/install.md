# Install

You need a Linux host with Docker Engine and the Compose plugin
(2 CPU, 2 GB RAM and 10 GB disk are plenty; add 2 GB of disk for Grafana). No Docker yet? Install it from Docker's own repository,
following https://docs.docker.com/engine/install/ for your distribution (Debian:
https://docs.docker.com/engine/install/debian/), then add your user to the `docker` group
(`sudo usermod -aG docker $USER`) and log in again.

```sh
mkdir homewatch && cd homewatch
for f in docker-compose.yml Caddyfile env.example install.sh update.sh; do
  curl -fsSLO "https://github.com/nikosiaf/homewatch/releases/latest/download/$f"
done
chmod +x install.sh update.sh
less install.sh
./install.sh
```

Please read `install.sh` before running it (`less install.sh`, `q` to quit); it's short.

`install.sh` does five things:
- writes `.env` with a new master key, the host's time zone, and its gateway and DNS server as defaults
- creates a self-signed certificate in `certs/`
- offers the updater (asks first, needs sudo once): a systemd unit that installs the latest release when
  you press Update on the System page (see [Update](update-and-backup.md#update))
- asks whether to add [Grafana](grafana.md) (default no; `--grafana` / `--no-grafana`
  answer without asking, and the System page can turn it on or off later)
- runs `docker compose up -d`

homeWatch listens on port **5443** (HTTPS) and **5080** (HTTP, which only redirects to HTTPS), so it
doesn't clash with a web server already on 80 or 443. Other ports: `./install.sh --https-port 8443
--http-port 8080`, or change `HW_HTTPS_PORT` and `HW_HTTP_PORT` in `.env` and run `docker compose up -d`.
`install.sh` stops if a port is already in use. With `HW_HTTPS_PORT=443` the address is just
`https://<host>`.

At the end it prints the address and the certificate's SHA-256 fingerprint. Open `https://<host>:5443` and compare that
fingerprint when the browser warns about the certificate (once). Create the admin account right
away: until it exists (after an install or a reset), whoever opens the page first creates it.
Then follow the setup wizard:
1. Admin account
2. Home name and time zone
3. Internet probes and contracted speed
4. Your equipment: tick what you have (modem, firewall, switches, Wi-Fi, servers, NAS, Home Assistant,
   UPS, Docker hosts); the wizard then opens each one's module page in turn, with its setup steps and
   Connect, and you can skip any of them
5. A notification channel
6. Heartbeat

## Manual install (without install.sh)

```sh
cp env.example .env && chmod 600 .env    # .env.example in a git checkout
openssl rand -base64 32          # put it in .env as HW_MASTER_KEY=…; also set TZ
mkdir certs && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj "/CN=homewatch" -addext "subjectAltName=IP:<host ip>"
docker compose up -d             # then open https://<host>:5443 (HW_HTTPS_PORT in .env)
```

## Behind a reverse proxy

If browsers reach homeWatch through a reverse proxy (HAProxy, nginx…), put the proxy's address in
`HW_TRUSTED_PROXIES` in `.env`, for example `HW_TRUSTED_PROXIES=192.168.1.1`, and run
`docker compose up -d`. The proxy must send `X-Forwarded-For`. Without this setting every visitor
counts as the proxy's address, so one person's failed logins lock everyone out for 5 minutes.
Don't list anything that isn't a proxy: whoever is listed can claim to be any address.

---

[← homeWatch README](../README.md) · [All documentation](README.md)
