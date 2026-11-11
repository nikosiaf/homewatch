# Security notes

- **Exposure:** don't forward homeWatch to the internet. Use it on your LAN or over Tailscale.
- **Accounts:** an **admin** can change everything; a **viewer** can only see status, history and help.
- **Outbound:** besides the devices you add, homeWatch reaches only the internet servers you set it to
  use: the public servers the Internet module pings and asks for DNS lookups, the speed-test service,
  your notification channels, the MQTT broker if you set one, GitHub for the daily release check (which
  you can turn off) and, if you tick it, `upgrade.mikrotik.com` once a day for the newest RouterOS
  version.
- **Network:** only Caddy publishes ports: HTTPS on 5443 and HTTP on 5080, which only redirects to HTTPS
  (`HW_HTTPS_PORT` / `HW_HTTP_PORT` in `.env`). VictoriaMetrics, Telegraf, Grafana and
  the controller stay on internal Docker networks.
- **Grafana:** reached only through Caddy, which asks homeWatch who you are first. Grafana trusts the
  user name homeWatch hands it only from the small private network it shares with Caddy, homeWatch and
  VictoriaMetrics; Caddy removes those headers from what browsers send.
- **Secrets:** passwords, tokens and URLs (including the MQTT password) are encrypted at rest (AES-GCM,
  key from `.env`) and never shown again after saving.
- **API tokens** are stored only as hashes and can only read. **Buttons in notifications** are random
  one-time links (stored as hashes, valid 24 hours) that can only snooze or acknowledge one check.
- **Privileges:** each module uses a read-only account where the device has one. The exception is
  OPNsense's DHCP leases for the Map: the only privilege that opens them (Services: Dnsmasq DNS/DHCP:
  Settings) would also allow changing Dnsmasq's settings; homeWatch only reads the leases.
- **Logins:** passwords are hashed with Argon2. Sessions use HttpOnly, Secure, SameSite=Strict
  cookies, and the database holds only hashes of session tokens. Every form is CSRF-protected, and
  logins lock for 5 minutes after 5 failures from one address or 20 on one account.
- **Device certificates:** with "Check the HTTPS certificate" unticked, homeWatch pins the certificate
  it sees first and logs in only to that one. That first connection can't tell an impostor from the
  device, so compare the fingerprint on the module's page with the device. System lists how each
  device connection is protected.
- **Containers:** image versions are pinned, and the controller runs as a non-root user on a
  read-only filesystem.

---

[← homeWatch README](../README.md) · [All documentation](README.md)
