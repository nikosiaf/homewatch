# Grafana dashboards (optional)

Grafana shows everything homeWatch stores (every device, port, guest, disk, and the Home Assistant
entities you show in Grafana) at `https://<homewatch>/grafana/`. It has no login of its own: homeWatch signs
you in. homeWatch admins are Grafana admins, viewers can only look.

- **On or off:** `install.sh` asks; later, **System → Grafana and history**. It needs about 2 GB of
  disk and 150 MB of memory. Turning it off keeps its dashboards. By hand: `COMPOSE_PROFILES=grafana`
  in `.env` (empty to turn it off), then `docker compose up -d`.
- **Built-in dashboards** (folder *homeWatch*): an Overview, **Health** and one per enabled module.
  homeWatch rebuilds them with every update, so they can't be saved; switching a module off removes its
  dashboard.
- **Health:** each module's status over time, how much of the last 30 days each device could be read
  (the internet: the line was up), time to recover per check, the noisiest checks, and the incidents
  and device events as lists.
- **Markers:** incidents (a red band from start to end, with their checks and likely cause) and device
  events (reboots, resyncs, power cuts, gateway and PPPoE drops) are written to Grafana's own
  annotations at their exact time, as the Grafana user `homewatch-markers`; every dashboard shows them.
- **Internet quality:** latency under load (bufferbloat) and its grade, the slowest moment of each
  hour, speed as a share of the line's sync rate, ISP outages and modem events.
- **Storage forecast** (Overview): days until NAS volumes, backup datastores and Proxmox storage are
  full, at the rate of the last 7 days.
- **Your own dashboards:** open a built-in one, **Save as**, and keep the copy in **My dashboards**.
  Updates never touch that folder, and homeWatch saves it every night next to the configuration
  exports (System → Grafana and history, last 14).
- **From homeWatch:** an open Status tile has a small Grafana icon next to its line, and the menu has
  **Grafana ↗**; both open in a new tab and show only while Grafana runs.
- **Resolution** follows each module's [read interval](modules.md#how-often-each-module-is-read).
- **Data source:** *homewatch*, the VictoriaMetrics that homeWatch already runs (PromQL / MetricsQL).
  Metric names start with `hw_`; Grafana's Explore lists them.
- **History length:** a year by default. **System → History** can keep less (6, 3 or 1 month): data
  older than that is deleted, and VictoriaMetrics drops whole months, so space can take up to a month
  to come back. Issues, speed tests and settings are not affected.

---

[← homeWatch README](../README.md) · [All documentation](README.md)
