# Alerts and notifications

Notification providers: **Telegram**, **ntfy** (one topic URL), **email** (any SMTP server; port
465 = TLS, other ports STARTTLS, a password is never sent unencrypted), **webhook** and the
**healthchecks.io heartbeat**.

Alerting works like this:
- **Confirmation:** a change must hold for 2 runs (about 4 minutes) before it's sent.
- **Incidents and likely cause:** problems that start within 30 minutes of each other are one
  incident; "back to OK" arrives as one message when the whole incident is over. The modules note what
  their devices did, at the device's own time where it tells it (reboots, DSL resyncs, the UPS on
  battery, ports and gateways going down, PPPoE drops), and when an incident ends homeWatch names the
  likely cause: a power cut in the area or at home, a modem restart, a line or ISP problem, one of your
  own devices. History lists incidents with their timeline.
- **Events, not warnings:** a single DSL resync or device reboot is an event and a Grafana marker; a
  WARNING comes when it repeats (3 resyncs or 2 reboots in 24 h by default).
- **To do instead of problems:** updates and firmware (Home Assistant, PBS, DSM, UniFi, RouterOS and
  RouterBOOT, OPNsense) are listed under To do on the Status page and in the weekly report. They become a
  WARNING only after waiting 14 days (7 for OPNsense and RouterOS); the limit is editable per check.
- **Reminders:** open problems are repeated every 12 h (not for acknowledged ones).
- **Snooze and maintenance windows:** they hold notifications without losing the history.
- **Quiet hours** per channel: warnings wait until they end, errors are always sent.
- **Offline queue:** messages that can't be sent while the line is down go out when it's back, with the outage times.
- **Buttons in ntfy:** Snooze 1 day, Acknowledge (no more reminders) and Open homeWatch, as one-time
  links valid for 24 h. They need homeWatch's address (System → Home) reachable from the phone (home
  network or VPN); homeWatch is never opened to the internet for them.
- **What to do:** messages carry a line on what to check first; write your own per check under the
  module's **Checks and limits → What to do**.
- **Useful or noise:** mark problems on the History page; the weekly report lists the noisiest checks.
- **Weekly report:** optional; pick the day, the time and the channels. It has the week's uptime, line
  quality and speed, the longest issues, what is still to do and the checks with the most issues.
- **healthchecks.io heartbeat:** optional. homeWatch pings it after every run, and healthchecks.io
  alerts you when the pings stop (host, power or internet down).
- **Status page:** a module tile says "No new reading for …" when its device hasn't been read for 3 of
  its read intervals, so old results don't look current.
- **History** is a timeline of every problem and its return to OK.

## Webhook

The webhook channel POSTs every message as JSON to the URL you enter. The URL is stored encrypted
like a password (webhook URLs usually contain a token) and is never part of an export:

```json
{"text": "🔴 Home: ERROR\n\n…", "content": "🔴 Home: ERROR\n\n…", "title": "🔴 Home: ERROR",
 "body": "…", "level": 2, "level_name": "ERROR", "sent": 1793260000}
```

`text` makes it work as-is with Slack, Mattermost and Rocket.Chat incoming webhooks, `content` with
Discord (cut to 2000 characters). For Home Assistant, Node-RED or n8n use `level` / `level_name`,
`title` and `body`.

---

[← homeWatch README](../README.md) · [All documentation](README.md)
