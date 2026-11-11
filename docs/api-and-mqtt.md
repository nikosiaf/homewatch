# API and Home Assistant (MQTT)

## Home Assistant (MQTT)

**System → Home Assistant (MQTT)**: homeWatch publishes itself to the MQTT broker Home Assistant uses
(e.g. the Mosquitto add-on) with MQTT discovery, and appears as one device:

| Entity | What |
|---|---|
| Status | OK / WARNING / ERROR, with the error and warning counts |
| Problems, To do | how many are open |
| one problem sensor per module | on while that module has a problem |
| Alerts (event) | fires `problem` and `back_to_ok`, with the check, detail and likely cause, for automations |
| Check now (button) | runs the checks |

Make a broker user for homeWatch. States are retained; when homeWatch stops, the entities show unavailable.

## API

A read-only JSON API for scripts and other tools. Make a token under **System → API tokens** (it is
shown once) and send it as `Authorization: Bearer <token>`. A token can only read.

| Path | What |
|---|---|
| `GET /api/v1/status` | overall level, counts, open issues, to-dos, each module's level and last reading |
| `GET /api/v1/checks` | every check now: level, detail, value, since, pending, silenced |
| `GET /api/v1/issues?days=7&open=1&module=internet` | problems and their return to OK |
| `GET /api/v1/incidents?days=30` | incidents with their checks, likely cause and device events |
| `GET /api/v1/events?days=7` | what the devices did |
| `GET /api/v1/todos` | updates and firmware waiting |

Times are Unix seconds; levels are 0 OK, 1 WARNING, 2 ERROR (`level_word` has the name).

```sh
curl -sk -H "Authorization: Bearer hw_…" https://homewatch.local/api/v1/status
```

---

[← homeWatch README](../README.md) · [All documentation](README.md)
