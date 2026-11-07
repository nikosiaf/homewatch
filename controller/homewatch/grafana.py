"""Grafana (optional): the controller writes Grafana's provisioning into a shared volume.

The data source (homewatch's VictoriaMetrics) and the built-in dashboards are files, so Grafana loads them at
start and reloads the dashboards when they change. Built-in dashboards can't be saved from the Grafana UI
(allowUiUpdates false): "Save as" puts a copy in "My dashboards", which updates never touch. Only the
dashboards of enabled modules are written.
"""

from __future__ import annotations

import json
import os
import re

DS = {"type": "prometheus", "uid": "homewatch-vm"}
S = 'site="$site"'  # every query is for the chosen site
FOLDER = "homeWatch"  # the folder's title; its uid stays "homewatch"
MY_FOLDER = "My dashboards"

STATUS_MAP = [{"type": "value", "options": {
    "0": {"text": "✔ OK", "color": "green", "index": 0},
    "1": {"text": "⚠ WARNING", "color": "yellow", "index": 1},
    "2": {"text": "✖ ERROR", "color": "red", "index": 2}}}]
OK_ZERO = {"type": "value", "options": {"0": {"text": "✔ OK", "color": "green", "index": 0}}}
# Grafana puts its own /grafana in front of every link that starts with "/"; Caddy sends
# /grafana/homewatch/open/<module> (opened in a new tab) to homewatch's Status page with that module's tile open
HOMEWATCH = "/grafana/homewatch"
QUIET = "#3d4450"  # the normal, uneventful state in a timeline (closed, idle, stopped on purpose)
UPDOWN_MAP = [{"type": "value", "options": {"0": {"text": "down", "color": "red", "index": 0},
                                             "1": {"text": "up", "color": "green", "index": 1}}}]


# ---- panels ---------------------------------------------------------------------------------------

def _targets(exprs):
    return [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": legend, "range": True}
            for i, (e, legend) in enumerate(exprs)]


def ts(title, exprs, unit="short", w=12, h=8, stack=False, minimum=None, maximum=None, desc="", fill=10, lw=1,
       calcs=()):
    """A time series panel; exprs = [(PromQL/MetricsQL, legend)]; calcs = legend values (mean, max…)."""
    custom = {"lineWidth": lw, "fillOpacity": fill, "showPoints": "never", "spanNulls": 3 * 3600000,
              "stacking": {"mode": "normal" if stack else "none"}}
    defaults = {"unit": unit, "custom": custom}
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS, "_w": w, "_h": h,
            "targets": _targets(exprs), "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "calcs": list(calcs)},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def stat(title, expr, unit="short", legend="", w=6, h=4, steps=None, mappings=None, desc="", decimals=None,
         text_mode="auto", none=None, color_mode="background"):
    """A stat panel; steps = [(value, colour)] thresholds from the lowest (value None = base). A count of problems
    (fine at 0, coloured from 1) shows ✔ OK at 0."""
    problems = bool(steps) and len(steps) > 1 and steps[0][1] in ("green", "text") and steps[1][0] == 1
    defaults = {"unit": unit, "mappings": (mappings or []) + ([OK_ZERO] if problems else []),
                "thresholds": {"mode": "absolute",
                               "steps": [{"value": v, "color": c} for v, c in (steps or [(None, "green")])]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    if none is not None:  # what to show when the query finds nothing (e.g. "none" for a list of problems)
        defaults["noValue"] = none
    return {"type": "stat", "title": title, "description": desc, "datasource": DS, "_w": w, "_h": h,
            "targets": [{"refId": "A", "datasource": DS, "expr": expr, "legendFormat": legend, "instant": True}],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": color_mode, "graphMode": "none", "textMode": text_mode,
                        "justifyMode": "center", "orientation": "auto"}}


def gauge(title, expr, unit="percent", legend="", w=6, h=5, warn=80, error=90, maximum=100):
    """Bars that turn yellow/red at warn/error; both None: lower is worse (red under 10, yellow under 20)."""
    steps = ([{"value": None, "color": "green"}, {"value": warn, "color": "yellow"}, {"value": error, "color": "red"}]
             if warn is not None else
             [{"value": None, "color": "red"}, {"value": 10, "color": "yellow"}, {"value": 20, "color": "green"}])
    return {"type": "bargauge", "title": title, "datasource": DS, "_w": w, "_h": h,
            "targets": [{"refId": "A", "datasource": DS, "expr": expr, "legendFormat": legend, "instant": True}],
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "max": maximum,
                                         "thresholds": {"mode": "absolute", "steps": steps}},
                "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True}}


def table(title, exprs, columns, w=24, h=8, desc="", hide=(), mappings=None, links=None):
    """Instant queries joined on their labels; columns = {label or 'Value #A': header} in order."""
    t = [{"refId": chr(65 + i), "datasource": DS, "expr": e, "instant": True, "format": "table"}
         for i, e in enumerate(exprs)]
    order = {k: i for i, k in enumerate(columns)}
    return {"type": "table", "title": title, "description": desc, "datasource": DS, "_w": w, "_h": h, "targets": t,
            "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True}}},
                                {"id": "merge", "options": {}},
                                {"id": "organize", "options": {
                                    "excludeByName": {"Time": True, "__name__": True, "site": True,
                                                      **{k: True for k in hide}},
                                    "indexByName": order, "renameByName": columns}}],
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {
                "type": "color-background" if mappings else "auto"}}, "mappings": mappings or []},
                "overrides": [{"matcher": {"id": "byName", "options": field}, "properties": [
                    {"id": "links", "value": [{"title": title_, "url": url, "targetBlank": True}]}]}
                    for field, (title_, url) in (links or {}).items()]},
            "options": {"showHeader": True, "cellHeight": "sm"}}


def timeline(title, exprs, w=24, h=8, mappings=None):
    return {"type": "state-timeline", "title": title, "datasource": DS, "_w": w, "_h": h, "targets": _targets(exprs),
            "fieldConfig": {"defaults": {"mappings": mappings or UPDOWN_MAP, "custom": {"fillOpacity": 80}}, "overrides": []},
            "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                        "legend": {"showLegend": False}}}


def row(title, collapsed=False):
    return {"type": "row", "title": title, "collapsed": collapsed, "panels": [], "_w": 24, "_h": 1}


def _layout(panels):
    """Place panels left to right, wrapping at 24 columns; a row starts a new line. The panels after a collapsed
    row go inside it (placed as they will be when it opens) until the next row."""
    out, x, y, line_h, folded = [], 0, 0, 0, None
    for i, p in enumerate(panels):
        w, h = p.pop("_w"), p.pop("_h")
        if p["type"] == "row":
            if folded:  # the folded panels take no room on the page
                y = folded["gridPos"]["y"] + 1
            x, y, line_h = 0, y + line_h if not folded else y, 0
            folded = p if p.get("collapsed") else None
        elif x + w > 24:
            x, y, line_h = 0, y + line_h, 0
        p["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        p["id"] = i + 1
        x += w
        line_h = max(line_h, h)
        if p["type"] == "row":
            x, y, line_h = 0, y + h, 0
            out.append(p)
        elif folded:
            folded["panels"].append(p)
        else:
            out.append(p)
    return out


def dashboard(uid, title, panels, desc="", tags=(), time="now-24h"):
    back = [] if uid == "hw-overview" else [
        {"type": "link", "title": "Back to overview", "url": "/grafana/d/hw-overview", "targetBlank": False,
         "icon": "dashboard", "keepTime": True, "includeVars": True}]
    return {"uid": uid, "title": title, "description": desc, "tags": ["homewatch", *tags], "timezone": "browser",
            "editable": True, "graphTooltip": 1, "refresh": "1m", "schemaVersion": 41,
            "time": {"from": time, "to": "now"},
            "links": [{"type": "dashboards", "tags": ["homewatch"], "asDropdown": True, "title": "homewatch",
                       "includeVars": True, "keepTime": True},
                      *back],
            "templating": {"list": [{"name": "site", "label": "Site", "type": "query", "datasource": DS,
                                     "query": {"query": "label_values(hw_run_ok, site)", "refId": "site"},
                                     "definition": "label_values(hw_run_ok, site)", "refresh": 1,
                                     "hide": 2, "includeAll": False, "multi": False}]},
            "panels": _layout(panels)}


# ---- the dashboards -------------------------------------------------------------------------------

def _pct_steps(warn=80, error=90):
    return [(None, "green"), (warn, "yellow"), (error, "red")]


def overview(enabled: set[str], ha: list[dict] | None = None) -> dict:
    on = enabled.__contains__
    p = [row("Status"),
         stat("Problems now", f"count(hw_check_status_value{{{S}}} > 0) or vector(0)", w=4, h=5,
              steps=[(None, "green"), (1, "red")]),
         stat("Modules", f"max by (module) (last_over_time(hw_check_status_value{{{S}}}[10m]))", legend="{{module}}",
              w=20, h=5, mappings=STATUS_MAP, steps=[(None, "green"), (1, "yellow"), (2, "red")],
              text_mode="value_and_name"),
         table("Needs attention", [f"hw_check_status_value{{{S}}} > 0"], {"module": "Module", "check": "Check", "Value": "Level"},
               w=8, h=7, mappings=STATUS_MAP, links={"Module": ("Open in homewatch", HOMEWATCH + "/open/${__value.raw}")},
               desc="Checks that are WARNING or ERROR now (the number in Problems now). Click a module to open its "
                    "tile on homewatch's Status page. Empty: all is well."),
         ts("Checks not OK", [(f"count by (module) (hw_check_status_value{{{S}}} > 0)", "{{module}}")], w=16, h=7,
            stack=True, minimum=0, fill=60, desc="How many checks were WARNING or ERROR, per module.")]
    # Internet: WAN traffic on top, then the line's numbers and graphs, then the modem
    def tile(title, expr, unit="short", steps=None, **kw):
        return stat(title, expr, unit, w=4, color_mode="value", steps=steps or [(None, "text")], **kw)

    def graph(title, exprs, unit, w=12, **kw):
        return ts(title, exprs, unit, w=w, lw=2, fill=12, calcs=("mean", "max"), **kw)

    if on("internet") or on("speedport") or on("opnsense"):
        p.append(row("Internet"))
    if on("opnsense"):
        p.append(graph("WAN traffic", [
            (f'rate(hw_opnsense_interface_rx_bytes{{{S},interface=~"(?i)wan.*"}}) * 8', "in {{interface}}"),
            (f'-rate(hw_opnsense_interface_tx_bytes{{{S},interface=~"(?i)wan.*"}}) * 8', "out {{interface}}")], "bps", w=24, h=7))
    if on("internet"):
        p += [tile("WAN latency", f'avg(ping_average_response_ms{{{S},role="public"}})', "ms",
                   [(None, "text"), (60, "yellow"), (100, "red")]),
              tile("Packet loss", f'max(ping_percent_packet_loss{{{S},role="public"}})', "percent",
                   [(None, "text"), (10, "yellow"), (20, "red")]),
              tile("Speed down (last test)", f'last_over_time(hw_speedtest_download_mbps{{{S}}}[1d])', "Mbits"),
              tile("Speed up (last test)", f'last_over_time(hw_speedtest_upload_mbps{{{S}}}[1d])', "Mbits"),
              stat("WAN status", f'max(hw_check_status_value{{{S},module="internet"}})', w=4, mappings=STATUS_MAP,
                   steps=[(None, "green"), (1, "yellow"), (2, "red")],
                   desc="The worst of the Internet module's checks (reachable, loss, latency, jitter, DNS, speed)."),
              tile("WAN jitter", f'avg(ping_standard_deviation_ms{{{S},role="public"}})', "ms",
                   [(None, "text"), (15, "yellow"), (30, "red")]),
              graph("WAN latency", [(f'ping_average_response_ms{{{S}}}', "{{role}} {{url}}")], "ms", minimum=0),
              graph("Speed tests", [(f'hw_speedtest_download_mbps{{{S}}}', "down"), (f'hw_speedtest_upload_mbps{{{S}}}', "up")],
                    "Mbits", minimum=0)]
    if on("speedport"):
        dsl = lambda m: f"last_over_time(hw_dsl_{m}{{{S}}}[3h])"  # the modem is read every few hours at most
        p += [tile("VDSL sync down", dsl("sync_down_kbps") + " * 1000", "bps"),
              tile("VDSL sync up", dsl("sync_up_kbps") + " * 1000", "bps"),
              tile("Attainable down", dsl("attainable_down_kbps") + " * 1000", "bps"),
              tile("SNR margin down", dsl("snr_down_db"), "dB", [(None, "red"), (6, "yellow"), (9, "text")]),
              tile("Attenuation down", dsl("atten_down_db"), "dB"),
              tile("In sync for", dsl("line_up_s"), "s"),
              graph("VDSL downstream rate", [(f"hw_dsl_sync_down_kbps{{{S}}} * 1000", "sync"),
                                             (f"hw_dsl_attainable_down_kbps{{{S}}} * 1000", "attainable")], "bps", minimum=0),
              graph("VDSL noise margin", [(f"hw_dsl_snr_down_db{{{S}}}", "down"), (f"hw_dsl_snr_up_db{{{S}}}", "up")], "dB")]
    # Firewall, from the OPNsense module only
    if on("opnsense"):
        fw = lambda m: f"max(hw_opnsense_{m}{{{S}}})"  # noqa: E731 - one firewall per site
        p += [row("Firewall"),
              # colours as the module's CPU and Memory checks (default limits); memory without the ZFS cache, as the check
              tile("Firewall CPU", fw("system_cpu_percent"), "percent", [(None, "green"), (85, "yellow"), (95, "red")]),
              tile("Firewall memory", f"max(100 * (hw_opnsense_system_mem_used_bytes{{{S}}} - (hw_opnsense_system_arc_bytes{{{S}}} "
                                      f"or hw_opnsense_system_mem_used_bytes{{{S}}} * 0)) / hw_opnsense_system_mem_total_bytes{{{S}}})",
                   "percent", [(None, "green"), (90, "yellow"), (95, "red")], desc="Used memory without the ZFS cache (ARC)."),
              tile("Firewall uptime", fw("system_uptime_seconds"), "s"),
              tile("HAProxy servers down", f"sum(hw_opnsense_haproxy_down{{{S}}} * hw_opnsense_haproxy_watched{{{S}}}) or "
                                           f"sum(hw_opnsense_haproxy_down{{{S}}})", steps=[(None, "green"), (1, "red")],
                   desc="Watched HAProxy servers whose health check says DOWN, as the module's HAProxy check "
                        "(servers you unticked and servers without a check don't count)."),
              tile("Firewall disk", f"max(100 * hw_opnsense_disk_used_bytes{{{S}}} / hw_opnsense_disk_total_bytes{{{S}}})",
                   "percent", [(None, "green"), (85, "yellow"), (95, "red")], desc="Root disk, as the module's Root disk check."),
              tile("Gateway delay", f"max(hw_opnsense_gateway_delay_ms{{{S}}})", "ms", [(None, "text"), (60, "yellow"), (100, "red")],
                   desc="OPNsense's gateway monitor: round-trip time to the monitor address (the slowest gateway)."),
              graph("Received per interface", [(f"rate(hw_opnsense_interface_rx_bytes{{{S}}}) * 8", "{{interface}}")], "bps",
                    minimum=0, desc="Needs the OPNsense privilege Reporting: Traffic."),
              graph("Sent per interface", [(f"rate(hw_opnsense_interface_tx_bytes{{{S}}}) * 8", "{{interface}}")], "bps",
                    minimum=0, desc="Needs the OPNsense privilege Reporting: Traffic.")]
    if on("mikrotik") or on("unifi"):
        p.append(row("Network"))
    if on("mikrotik"):
        p += [gauge("Switch CPU", f"hw_mikrotik_device_cpu_percent{{{S}}}", legend="{{device}}", w=6, h=7, warn=80, error=95),
              ts("Busiest switch ports", [(f'topk(5, (rate(hw_mikrotik_port_rx_bytes{{{S}}}) + rate(hw_mikrotik_port_tx_bytes{{{S}}})) * 8)',
                                           "{{device}} {{port}} {{comment}}")], "bps", w=18, h=7, minimum=0,
                 desc="The five ports with the most traffic (in + out).")]
    if on("unifi"):
        p += [stat("Wi-Fi clients", f'sum(hw_unifi_device_clients{{{S}}})', w=4, h=7),
              ts("Clients per access point", [(f'hw_unifi_device_clients{{{S}}}', "{{device}}")], w=10, h=7, stack=True,
                 minimum=0, fill=40),
              ts("Traffic per access point", [(f'(rate(hw_unifi_device_rx_bytes{{{S}}}) + rate(hw_unifi_device_tx_bytes{{{S}}})) * 8',
                                               "{{device}}")], "bps", w=10, h=7, minimum=0)]
    if on("proxmox") or on("synology") or on("pbs"):
        p.append(row("Servers"))
    if on("proxmox"):
        p += [gauge("Proxmox CPU", f"hw_proxmox_node_cpu_percent{{{S}}}", legend="{{node}}", w=5, h=6, warn=85, error=95),
              gauge("Proxmox memory", f"100 * hw_proxmox_node_mem_used_bytes{{{S}}} / hw_proxmox_node_mem_total_bytes{{{S}}}",
                    legend="{{node}}", w=5, h=6, warn=85, error=92),
              gauge("Proxmox storage", f"100 * hw_proxmox_storage_used_bytes{{{S}}} / hw_proxmox_storage_total_bytes{{{S}}} > 0",
                    legend="{{storage}}", w=6, h=6, warn=80, error=90),
              stat("Guests running", f"sum(hw_proxmox_guest_running{{{S}}})", w=4, h=6)]
    if on("pbs"):
        p += [stat("Oldest backup", f"max((time() - hw_pbs_group_last_backup_timestamp{{{S}}}) / 3600 "
                                     f"and (time() - hw_pbs_group_last_backup_timestamp{{{S}}}) < 7 * 86400)", "h", w=4, h=6,
                   steps=[(None, "green"), (26, "yellow"), (50, "red")],
                   desc="Hours since the last backup of the least recent guest backed up this week.")]
    if on("proxmox"):  # guest and host CPU graphs, on their own line below the Proxmox tiles
        p += [ts("Guest CPU (% of one guest's vCPUs)",
                 [(f"hw_proxmox_guest_cpu_percent{{{S}}} and on (node, vmid) hw_proxmox_guest_running{{{S}}} == 1",
                   "{{name}}")], "percent", w=12, h=8, minimum=0, lw=2, fill=0, calcs=("mean",),
                 desc="CPU of each running VM and container, as a share of the vCPUs it was given."),
              graph("Host CPU", [(f"hw_proxmox_node_cpu_percent{{{S}}}", "{{node}}")], "percent", h=8, minimum=0,
                    desc="CPU of each Proxmox node.")]
    if on("synology"):
        p += [gauge("NAS volumes", f"100 * hw_synology_volume_used_bytes{{{S}}} / hw_synology_volume_total_bytes{{{S}}}",
                    legend="{{volume}}", w=6, h=5, warn=85, error=95),
              stat("NAS temperature", f"hw_synology_system_temp_celsius{{{S}}}", "celsius", w=6, h=5,
                   steps=[(None, "green"), (55, "yellow"), (65, "red")]),
              stat("Hottest disk", f"max(hw_synology_disk_temp_celsius{{{S}}})", "celsius", w=6, h=5,
                   steps=[(None, "green"), (50, "yellow"), (58, "red")]),
              ts("NAS network", [(f'sum(hw_synology_net_rx_bytes_per_second{{{S}}}) * 8', "in"),
                                 (f'-sum(hw_synology_net_tx_bytes_per_second{{{S}}}) * 8', "out")], "bps", w=6, h=5)]
    if on("homeassistant"):
        meta = sorted(unique_names(ha or []), key=lambda e: (e.get("name") or e["id"]).lower())
        doors, temps = _pick(meta, "binary_sensor", DOORS), _pick(meta, "sensor", "temperature")
        p.append(row("Home"))
        if doors:
            p.append(stat("Doors open", f"sum({hq(doors)}) or vector(0)", w=4, h=7, steps=[(None, "green"), (1, "orange")]))
        p += [stat("Home Assistant CPU", f'hw_check_value_value{{{S},check="homeassistant.cpu"}}', "percent", w=4, h=7,
                   steps=_pct_steps())]
        if temps:
            p.append(ts("Temperatures", [(hq(temps[:PER_PANEL]), "{{name}}")], "celsius", w=16, h=7))
    # UPS, from the UPS module only; last on the page
    if on("ups"):
        p += [row("UPS"),
              stat("UPS", f"hw_ups_on_battery{{{S}}}", w=4, color_mode="value", mappings=[{"type": "value", "options": {
                  "0": {"text": "✔ On mains", "color": "green"}, "1": {"text": "✖ ON BATTERY", "color": "red"}}}]),
              tile("Battery", f"hw_ups_battery_charge_percent{{{S}}}", "percent", [(None, "red"), (50, "yellow"), (80, "text")]),
              tile("Runtime", f"hw_ups_battery_runtime_seconds{{{S}}}", "s", [(None, "red"), (600, "yellow"), (900, "text")]),
              tile("Power draw", f"hw_ups_power_watts{{{S}}}", "watt", desc="The UPS's load in watts (load % × its nominal power)."),
              tile("Load", f"hw_ups_load_percent{{{S}}}", "percent", [(None, "text"), (60, "yellow"), (80, "red")]),
              tile("Input voltage", f"hw_ups_input_voltage_volts{{{S}}}", "volt", [(None, "red"), (207, "text"), (253, "red")],
                   decimals=0),
              graph("UPS load", [(f"hw_ups_load_percent{{{S}}}", "{{ups}}")], "percent", h=8, minimum=0, maximum=100),
              graph("Power draw", [(f"hw_ups_power_watts{{{S}}}", "{{ups}}")], "watt", h=8, minimum=0)]
    return dashboard("hw-overview", "homeWatch Overview", p, "Every module at a glance.", time="now-24h")


def internet() -> dict:
    return dashboard("hw-internet", "Internet", [
        row("Ping"),
        ts("Latency", [(f'ping_average_response_ms{{{S}}}', "{{role}} {{url}}")], "ms", minimum=0),
        ts("Jitter", [(f'ping_standard_deviation_ms{{{S}}}', "{{url}}")], "ms", minimum=0),
        ts("Packet loss", [(f'ping_percent_packet_loss{{{S}}}', "{{url}}")], "percent", minimum=0),
        ts("DNS lookup time", [(f'dns_query_query_time_ms{{{S}}}', "{{server}} {{domain}}")], "ms", minimum=0),
        row("Speed tests"),
        ts("Download vs line sync", [(f'hw_speedtest_download_mbps{{{S}}}', "speed test {{engine}}"),
                                     (f'hw_dsl_sync_down_kbps{{{S}}} / 1000', "line sync (modem module)")], "Mbits", w=8,
           minimum=0),
        ts("Upload vs line sync", [(f'hw_speedtest_upload_mbps{{{S}}}', "speed test {{engine}}"),
                                   (f'hw_dsl_sync_up_kbps{{{S}}} / 1000', "line sync (modem module)")], "Mbits", w=8, minimum=0),
        ts("Latency under test", [(f'hw_speedtest_latency_ms{{{S}}}', "{{engine}}")], "ms", w=8, minimum=0),
    ], "Ping, DNS and speed tests.", time="now-7d")


def speedport() -> dict:
    return dashboard("hw-speedport", "Modem line", [
        stat("Line", f"last_over_time(hw_dsl_link_up{{{S}}}[3h])", mappings=UPDOWN_MAP, w=4, steps=[(None, "red"), (1, "green")]),
        stat("Sync down", f"last_over_time(hw_dsl_sync_down_kbps{{{S}}}[3h]) * 1000", "bps", w=5),
        stat("Sync up", f"last_over_time(hw_dsl_sync_up_kbps{{{S}}}[3h]) * 1000", "bps", w=5),
        stat("Line up for", f"last_over_time(hw_dsl_line_up_s{{{S}}}[3h])", "s", w=5),
        stat("Modem up for", f"last_over_time(hw_dsl_modem_uptime_s{{{S}}}[3h])", "s", w=5),
        ts("Sync and attainable speed", [(f'hw_dsl_sync_down_kbps{{{S}}} * 1000', "sync down"),
                                         (f'hw_dsl_attainable_down_kbps{{{S}}} * 1000', "attainable down"),
                                         (f'hw_dsl_sync_up_kbps{{{S}}} * 1000', "sync up"),
                                         (f'hw_dsl_attainable_up_kbps{{{S}}} * 1000', "attainable up")], "bps", w=24),
        ts("Noise margin (SNR)", [(f'hw_dsl_snr_down_db{{{S}}}', "down"), (f'hw_dsl_snr_up_db{{{S}}}', "up")], "dB"),
        ts("Attenuation", [(f'hw_dsl_atten_down_db{{{S}}}', "down"), (f'hw_dsl_atten_up_db{{{S}}}', "up")], "dB"),
        ts("Output power", [(f'hw_dsl_power_down_dbm{{{S}}}', "down"), (f'hw_dsl_power_up_dbm{{{S}}}', "up")], "dBm"),
        ts("Line uptime", [(f'hw_dsl_line_up_s{{{S}}}', "line"), (f'hw_dsl_modem_uptime_s{{{S}}}', "modem")], "s", minimum=0,
           desc="A drop to zero is a resync (line) or a restart (modem)."),
    ], "DSL line from the modem.", time="now-7d")


def opnsense() -> dict:
    f = 'firewall=~".+"'
    return dashboard("hw-opnsense", "OPNsense", [
        stat("CPU", f"hw_opnsense_system_cpu_percent{{{S}}}", "percent", w=6, steps=_pct_steps(70, 85)),
        stat("Memory", f"100 * hw_opnsense_system_mem_used_bytes{{{S}}} / hw_opnsense_system_mem_total_bytes{{{S}}}",
             "percent", w=6, steps=_pct_steps(85, 95)),
        stat("Disk", f"100 * hw_opnsense_disk_used_bytes{{{S}}} / hw_opnsense_disk_total_bytes{{{S}}}", "percent", w=6,
             steps=_pct_steps(80, 90)),
        stat("Gateways online", f"sum(hw_opnsense_gateway_online{{{S}}}) / count(hw_opnsense_gateway_online{{{S}}})",
             "percentunit", w=6, steps=[(None, "red"), (1, "green")]),
        row("Gateways"),
        ts("Gateway delay", [(f'hw_opnsense_gateway_delay_ms{{{S}}}', "{{gateway}}")], "ms", w=8, minimum=0),
        ts("Gateway jitter", [(f'hw_opnsense_gateway_stddev_ms{{{S}}}', "{{gateway}}")], "ms", w=8, minimum=0),
        ts("Gateway loss", [(f'hw_opnsense_gateway_loss_percent{{{S}}}', "{{gateway}}")], "percent", w=8, minimum=0),
        row("Traffic"),
        ts("Received per interface", [(f'rate(hw_opnsense_interface_rx_bytes{{{S},{f}}}) * 8', "{{interface}}")], "bps",
           desc="Needs the OPNsense privilege Reporting: Traffic."),
        ts("Sent per interface", [(f'rate(hw_opnsense_interface_tx_bytes{{{S},{f}}}) * 8', "{{interface}}")], "bps"),
        row("System"),
        ts("CPU", [(f'hw_opnsense_system_cpu_percent{{{S}}}', "CPU")], "percent", w=8, minimum=0),
        ts("Memory", [(f'hw_opnsense_system_mem_used_bytes{{{S}}}', "used"), (f'hw_opnsense_system_arc_bytes{{{S}}}', "ZFS ARC")],
           "bytes", w=8, minimum=0),
        ts("HAProxy sessions", [(f'hw_opnsense_haproxy_sessions{{{S}}}', "{{backend}}/{{server}}")], w=8, minimum=0),
        timeline("HAProxy servers", [(f'hw_opnsense_haproxy_up{{{S}}}', "{{backend}}/{{server}}")], h=12),
        timeline("Services", [(f'hw_opnsense_service_running{{{S}}}', "{{service}}")], h=14),
    ], "Firewall, gateways and interface traffic.")


def proxmox() -> dict:
    g = f'hw_proxmox_guest_running{{{S}}} == 1'
    return dashboard("hw-proxmox", "Proxmox VE", [
        row("Hosts"),
        gauge("Host CPU", f"hw_proxmox_node_cpu_percent{{{S}}}", legend="{{node}}", w=5, h=7, warn=85, error=95),
        gauge("Host memory", f"100 * hw_proxmox_node_mem_used_bytes{{{S}}} / hw_proxmox_node_mem_total_bytes{{{S}}}",
              legend="{{node}}", w=5, h=7, warn=85, error=92),
        gauge("Storage", f"100 * hw_proxmox_storage_used_bytes{{{S}}} / hw_proxmox_storage_total_bytes{{{S}}} > 0",
              legend="{{storage}}", w=6, h=7, warn=80, error=90),
        stat("Guests running", f"sum(hw_proxmox_guest_running{{{S}}})", w=4, h=7),
        stat("Host uptime", f"hw_proxmox_node_uptime_seconds{{{S}}}", "s", legend="{{node}}", w=4, h=7),
        ts("Host CPU", [(f'hw_proxmox_node_cpu_percent{{{S}}}', "{{node}}")], "percent", minimum=0),
        ts("Host memory", [(f'hw_proxmox_node_mem_used_bytes{{{S}}}', "{{node}} used"),
                           (f'hw_proxmox_node_mem_total_bytes{{{S}}}', "{{node}} total")], "bytes", minimum=0, fill=0),
        row("Guests"),
        ts("Guest CPU", [(f'hw_proxmox_guest_cpu_percent{{{S}}} and on(vmid) ({g})', "{{name}}")], "percent", minimum=0),
        ts("Guest memory", [(f'hw_proxmox_guest_mem_used_bytes{{{S}}} and on(vmid) ({g})', "{{name}}")], "bytes",
           stack=True, minimum=0, fill=40),
        ts("Guest network in", [(f'rate(hw_proxmox_guest_net_in_bytes{{{S}}}) * 8', "{{name}}")], "bps", minimum=0),
        ts("Guest network out", [(f'rate(hw_proxmox_guest_net_out_bytes{{{S}}}) * 8', "{{name}}")], "bps", minimum=0),
        ts("Guest disk read", [(f'rate(hw_proxmox_guest_disk_read_bytes{{{S}}})', "{{name}}")], "Bps", minimum=0),
        ts("Guest disk write", [(f'rate(hw_proxmox_guest_disk_write_bytes{{{S}}})', "{{name}}")], "Bps", minimum=0),
        timeline("Guests running", [(f'hw_proxmox_guest_running{{{S}}}', "{{name}}")], h=9, mappings=[
            {"type": "value", "options": {"0": {"text": "stopped", "color": QUIET}, "1": {"text": "running", "color": "green"}}}]),
        row("Storage and disks"),
        ts("Storage used", [(f'100 * hw_proxmox_storage_used_bytes{{{S}}} / hw_proxmox_storage_total_bytes{{{S}}} > 0',
                             "{{storage}}")], "percent", minimum=0, maximum=100, fill=0),
        table("Disk health", [f'hw_proxmox_disk_healthy{{{S}}}'],
              {"node": "Node", "disk": "Disk", "model": "Model", "Value": "SMART OK (1 = yes)"}, w=6),
        gauge("SSD life left", f"hw_proxmox_disk_life_left_percent{{{S}}}", legend="{{disk}} {{model}}", w=6, h=8,
              warn=None, error=None),
        table("Last backup per guest", [f'(time() - hw_proxmox_backup_last_ok_timestamp{{{S}}}) / 3600 * on(vmid) '
                                        f'group_left(name) max by (vmid, name) (hw_proxmox_guest_running{{{S}}} * 0 + 1)'],
              {"vmid": "ID", "name": "Guest", "Value": "Hours since the last good backup"}, w=12),
    ], "Host, guests, storage and backups.")


def mikrotik() -> dict:
    return dashboard("hw-mikrotik", "MikroTik", [
        row("Devices"),
        gauge("CPU", f"hw_mikrotik_device_cpu_percent{{{S}}}", legend="{{device}}", w=8, warn=80, error=95),
        gauge("Memory", f"hw_mikrotik_device_mem_percent{{{S}}}", legend="{{device}}", w=8, warn=80, error=90),
        stat("Temperature", f"hw_mikrotik_device_temp_celsius{{{S}}}", "celsius", legend="{{device}}", w=8, h=5,
             steps=[(None, "green"), (60, "yellow"), (70, "red")]),
        ts("CPU", [(f'hw_mikrotik_device_cpu_percent{{{S}}}', "{{device}}")], "percent", w=8, minimum=0),
        ts("Temperature", [(f'hw_mikrotik_device_temp_celsius{{{S}}}', "{{device}}")], "celsius", w=8),
        ts("Voltage", [(f'hw_mikrotik_device_voltage_volts{{{S}}}', "{{device}}")], "volt", w=8),
        ts("Memory", [(f'hw_mikrotik_device_mem_percent{{{S}}}', "{{device}}")], "percent", w=16, h=6, minimum=0),
        stat("Uptime", f"hw_mikrotik_device_uptime_seconds{{{S}}}", "s", legend="{{device}}", w=8, h=6,
             text_mode="value_and_name"),
        row("Ports"),
        ts("Received per port", [(f'rate(hw_mikrotik_port_rx_bytes{{{S}}}) * 8 > 0', "{{device}} {{port}} {{comment}}")], "bps"),
        ts("Sent per port", [(f'rate(hw_mikrotik_port_tx_bytes{{{S}}}) * 8 > 0', "{{device}} {{port}} {{comment}}")], "bps"),
        timeline("Ports up", [(f'hw_mikrotik_port_up{{{S}}} and on(device, port) '
                               f'max_over_time(hw_mikrotik_port_up{{{S}}}[7d]) == 1', "{{device}} {{port}} {{comment}}")],
                 h=12),
        ts("Errors per hour", [(f'increase(hw_mikrotik_port_errors{{{S}}}[1h]) > 0', "{{device}} {{port}}")], w=12, minimum=0),
        ts("Drops per hour", [(f'increase(hw_mikrotik_port_rx_drops{{{S}}}[1h]) + increase(hw_mikrotik_port_tx_drops{{{S}}}[1h]) > 0',
                               "{{device}} {{port}}")], w=12, minimum=0),
    ], "Switch health and per-port traffic.")


def unifi() -> dict:
    return dashboard("hw-unifi", "UniFi", [
        stat("Wi-Fi clients", f'sum(hw_unifi_device_clients{{{S}}})', w=6),
        stat("Devices online", f'sum(hw_unifi_device_online{{{S}}})', w=6),
        stat("Updates", f'sum(hw_unifi_device_upgradable{{{S}}})', w=6, steps=[(None, "green"), (1, "yellow")]),
        stat("Health", f'min(hw_unifi_health_ok{{{S}}})', w=6, mappings=[{"type": "value", "options": {
            "0": {"text": "✖ problem", "color": "red"}, "1": {"text": "✔ OK", "color": "green"}}}]),
        ts("Clients per device", [(f'hw_unifi_device_clients{{{S}}}', "{{device}}")], stack=True, minimum=0, fill=40),
        ts("Clients per radio", [(f'hw_unifi_radio_clients{{{S}}}', "{{device}} {{radio}}")], minimum=0),
        ts("Channel use", [(f'hw_unifi_radio_channel_use_percent{{{S}}}', "{{device}} {{radio}}")], "percent", minimum=0),
        ts("Traffic", [(f'rate(hw_unifi_device_rx_bytes{{{S}}}) * 8', "{{device}} in"),
                       (f'-rate(hw_unifi_device_tx_bytes{{{S}}}) * 8', "{{device}} out")], "bps"),
        ts("CPU", [(f'hw_unifi_device_cpu_percent{{{S}}}', "{{device}}")], "percent", w=8, minimum=0),
        ts("Memory", [(f'hw_unifi_device_mem_percent{{{S}}}', "{{device}}")], "percent", w=8, minimum=0),
        ts("Satisfaction", [(f'hw_unifi_device_satisfaction_percent{{{S}}}', "{{device}}")], "percent", w=8, maximum=100),
        timeline("Devices online", [(f'hw_unifi_device_online{{{S}}}', "{{device}}")], h=6),
    ], "Access points, clients and radios.")


def synology() -> dict:
    return dashboard("hw-synology", "Synology", [
        stat("Temperature", f"hw_synology_system_temp_celsius{{{S}}}", "celsius", w=6,
             steps=[(None, "green"), (55, "yellow"), (65, "red")]),
        stat("CPU", f"hw_synology_system_cpu_percent{{{S}}}", "percent", w=6, steps=_pct_steps()),
        stat("Memory", f"hw_synology_system_mem_percent{{{S}}}", "percent", w=6, steps=_pct_steps(85, 95)),
        stat("Hardware", f"min(hw_synology_system_system_ok{{{S}}}) * min(hw_synology_system_power_ok{{{S}}} or vector(1))",
             w=6, mappings=[{"type": "value", "options": {"0": {"text": "fault", "color": "red"},
                                                           "1": {"text": "✔ OK", "color": "green"}}}],
             desc="System and power supply status (needs SNMP in the module settings)."),
        gauge("Volumes", f"100 * hw_synology_volume_used_bytes{{{S}}} / hw_synology_volume_total_bytes{{{S}}}",
              legend="{{volume}}", w=12, h=6, warn=85, error=95),
        stat("Disk health", f"hw_synology_disk_healthy{{{S}}}", legend="{{disk}}", w=6, h=6, text_mode="value_and_name",
             mappings=[{"type": "value", "options": {"0": {"text": "✖ FAULT", "color": "red"},
                                                      "1": {"text": "✔ OK", "color": "green"}}}]),
        stat("Bad sectors", f"hw_synology_disk_bad_sectors{{{S}}}", legend="{{disk}}", w=6, h=6, text_mode="value_and_name",
             steps=[(None, "green"), (1, "yellow"), (50, "red")]),
        ts("Disk temperatures", [(f'hw_synology_disk_temp_celsius{{{S}}}', "{{disk}}"),
                                 (f'hw_synology_system_temp_celsius{{{S}}}', "system")], "celsius"),
        ts("CPU and memory", [(f'hw_synology_system_cpu_percent{{{S}}}', "CPU"),
                              (f'hw_synology_system_mem_percent{{{S}}}', "memory")], "percent", minimum=0, maximum=100),
        ts("Network", [(f'hw_synology_net_rx_bytes_per_second{{{S}}} * 8', "{{interface}} in"),
                       (f'-hw_synology_net_tx_bytes_per_second{{{S}}} * 8', "{{interface}} out")], "bps"),
        ts("Disk throughput", [(f'hw_synology_system_disk_read_bytes_per_second{{{S}}}', "read"),
                               (f'hw_synology_system_disk_write_bytes_per_second{{{S}}}', "write")], "Bps", minimum=0),
        ts("Volume used", [(f'hw_synology_volume_used_bytes{{{S}}}', "{{volume}}")], "bytes", w=24, h=6, fill=0),
    ], "NAS health, disks and volumes.")


def pbs() -> dict:
    return dashboard("hw-pbs", "Proxmox Backup Server", [
        gauge("Datastores", f"100 * hw_pbs_store_used_bytes{{{S}}} / hw_pbs_store_total_bytes{{{S}}}", legend="{{store}}",
              w=9, warn=80, error=90),
        stat("Deduplication factor", f"hw_pbs_store_dedup_factor{{{S}}}", legend="{{store}}", w=5, decimals=2,
             steps=[(None, "text")], color_mode="value",
             desc="All backups' size over what they take on disk, from the last garbage collection (as the PBS GUI)."),
        stat("Jobs failed (24 h)", f"sum(hw_pbs_jobs_failed_24h{{{S}}})", w=5, steps=[(None, "green"), (1, "red")]),
        stat("Updates", f"hw_pbs_system_updates{{{S}}}", w=5, steps=[(None, "green"), (1, "yellow")]),
        table("Backup groups", [f'(time() - hw_pbs_group_last_backup_timestamp{{{S}}}) / 3600', f'hw_pbs_group_snapshots{{{S}}}'],
              {"store": "Datastore", "type": "Type", "id": "ID", "name": "Name", "Value #A": "Hours since the last backup",
               "Value #B": "Snapshots"}, h=10),
        ts("Datastore used", [(f'hw_pbs_store_used_bytes{{{S}}}', "{{store}}")], "bytes", fill=0),
        ts("Jobs (24 h)", [(f'hw_pbs_jobs_ok_24h{{{S}}}', "{{type}} OK"), (f'hw_pbs_jobs_failed_24h{{{S}}}', "{{type}} failed")]),
    ], "Datastores, backup groups and jobs.", time="now-7d")


def ups() -> dict:
    return dashboard("hw-ups", "UPS", [
        stat("Status", f"hw_ups_on_battery{{{S}}}", w=6, mappings=[{"type": "value", "options": {
            "0": {"text": "on mains", "color": "green"}, "1": {"text": "ON BATTERY", "color": "red"}}}]),
        stat("Battery", f"hw_ups_battery_charge_percent{{{S}}}", "percent", w=6, steps=[(None, "red"), (50, "yellow"), (80, "green")]),
        stat("Runtime", f"hw_ups_battery_runtime_seconds{{{S}}}", "s", w=6, steps=[(None, "red"), (600, "yellow"), (1200, "green")]),
        stat("Load", f"hw_ups_load_percent{{{S}}}", "percent", w=6, steps=_pct_steps()),
        ts("Power draw", [(f'hw_ups_power_watts{{{S}}}', "{{ups}}")], "watt", minimum=0),
        ts("Load", [(f'hw_ups_load_percent{{{S}}}', "{{ups}}")], "percent", minimum=0),
        ts("Battery", [(f'hw_ups_battery_charge_percent{{{S}}}', "charge %")], "percent", minimum=0, maximum=100),
        ts("Voltages", [(f'hw_ups_input_voltage_volts{{{S}}}', "input"), (f'hw_ups_output_voltage_volts{{{S}}}', "output"),
                        (f'hw_ups_battery_voltage_volts{{{S}}}', "battery")], "volt"),
        timeline("On battery", [(f'hw_ups_on_battery{{{S}}}', "{{ups}}")], h=4, mappings=[{"type": "value", "options": {
            "0": {"text": "mains", "color": "green"}, "1": {"text": "battery", "color": "red"}}}]),
    ], "Power, battery and load.", time="now-7d")


# containers that still exist at the end of the shown range: removed ones (and throwaway `docker run --rm`
# containers) drop out of the panels, while the ones still there keep their whole history
NOW_CONTAINERS = f"(last_over_time(hw_docker_container_running{{{S}}}[5m]) @ end())"


def docker() -> dict:
    return dashboard("hw-docker", "Docker", [
        stat("Containers running", f"sum(hw_docker_container_running{{{S}}})", w=8),
        stat("Not running", f"count(hw_docker_container_running{{{S}}} == 0) or vector(0)", w=8,
             steps=[(None, "green"), (1, "yellow")]),
        stat("Unhealthy", f"count(hw_docker_container_healthy{{{S}}} == 0) or vector(0)", w=8,
             steps=[(None, "green"), (1, "red")]),
        timeline("Running", [(f'hw_docker_container_running{{{S}}} and on(host, container) {NOW_CONTAINERS}',
                              "{{host}} {{container}}")], h=14),
        ts("Restarts", [(f'(increase(hw_docker_container_restarts{{{S}}}[1h]) > 0) and on(host, container) {NOW_CONTAINERS}',
                         "{{container}}")], w=24, minimum=0),
    ], "Containers per host.")


# ---- Home Assistant: built from the recorded entities (names and units as Home Assistant has them now) --------

UNITS = {"°C": "celsius", "°F": "fahrenheit", "%": "percent", "W": "watt", "kW": "kwatt", "kWh": "kwatth",
         "Wh": "watth", "V": "volt", "A": "amp", "lx": "lux", "hPa": "pressurehpa", "mbar": "pressurembar", "bar": "pressurebar", "psi": "pressurepsi",
         "ppm": "ppm", "dB": "dB", "dBm": "dBm", "s": "s", "min": "m", "h": "h", "B": "bytes", "MB": "decmbytes",
         "GB": "decgbytes", "Mbit/s": "Mbits", "kB/s": "KBs", "MB/s": "MBs", "Hz": "hertz", "m³": "m3",
         "L": "litre", "µg/m³": "µg/m3", "km/h": "velocitykmh", "mm": "lengthmm", "km": "lengthkm"}
NUMERIC = {"sensor", "number", "input_number", "counter"}
DOORS = {"door", "window", "opening", "garage_door"}
SAFETY_DC = {"smoke", "moisture", "gas", "carbon_monoxide", "heat", "safety"}
PER_PANEL = 12  # series per auto-built panel
LONG = 40  # entities per query at most: names travel in the query, which VictoriaMetrics caps at 16 KB


def unique_names(meta: list[dict]) -> list[dict]:
    """Entities that share a name (two disks of the same model, say) get a part of their id after it, the one
    nearest the end that tells them apart: "Disk Temperature (nvme0n1)", "Disk Temperature (nvme1n1)"."""
    by: dict[str, list] = {}
    for e in meta:
        by.setdefault((e.get("name") or e["id"]).casefold(), []).append(e)
    out = []
    for e in meta:
        same = [o for o in by[(e.get("name") or e["id"]).casefold()] if o is not e]
        if not same:
            out.append(e)
            continue
        def parts(eid):  # the domain whole (binary_sensor), then the words of the object id
            dom, _, obj = eid.partition(".")
            return [dom, *obj.split("_")]

        mine, theirs = parts(e["id"]), [parts(o["id"]) for o in same]
        tag = next((mine[-i] for i in range(1, len(mine) + 1)
                    if all(len(o) < i or o[-i] != mine[-i] for o in theirs)), e["id"])
        out.append(e | {"name": f"{e.get('name') or e['id']} ({tag})"})
    return out


def _lit(text: str) -> str:
    """Text inside a "..." literal of a query; $ would be taken for a Grafana variable."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("$", "").replace("\n", " ")


def hq(ents: list[dict], metric="value", inner="{sel}") -> str:
    """A query over these entities, one series each (series written before 0.36 carried name/unit labels:
    max by entity_id joins them with the newer ones), with the entity's current name as the "name" label."""
    ids = "|".join(e["id"].replace(".", "\\\\.") for e in ents)
    sel = f'hw_ha_entity_{metric}{{{S},entity_id=~"{ids}"}}'
    pairs = ", ".join(f'"{e["id"]}", "{_lit(e.get("name") or e["id"])}"' for e in ents)
    return (f'label_map(label_copy(max by (entity_id) ({inner.replace("{sel}", sel)}), "entity_id", "name"), '
            f'"name", {pairs})')


def _pick(meta, domain=None, dc=None, field="value"):
    dcs = {dc} if isinstance(dc, str) else dc
    return [e for e in meta if field in e.get("fields", ("value",)) and (domain is None or e["domain"] == domain)
            and (dcs is None or e.get("dc") in dcs)]


def _chunks(ents, size=PER_PANEL):
    return [ents[i:i + size] for i in range(0, len(ents), size)] or [[]]


def _units(ents):
    """The entities per unit, the most common unit first: one panel can't show °C and °F, or bar and hPa."""
    out: dict[str, list] = {}
    for e in ents:
        out.setdefault(e.get("unit") or "", []).append(e)
    return sorted(out.items(), key=lambda kv: -len(kv[1]))


def _by_unit(title, ents, fallback, inner="{sel}", suffix="", **kw):
    """Time series panels for these entities: one per unit, at most PER_PANEL lines each."""
    groups, out = _units(ents), []
    for unit, part_all in groups:
        name = title + (f" ({unit})" if len(groups) > 1 and unit else "")
        parts = _chunks(part_all)
        for i, part in enumerate(parts):
            out.append(ts(_title(name, i, len(parts)), [(hq(part, inner=inner) + suffix, "{{name}}")],
                          UNITS.get(unit, fallback if len(groups) == 1 else "short"), **kw))
    return out


def _title(text, i, n):
    return f"{text} ({i + 1} of {n})" if n > 1 else text


ONOFF = [{"type": "value", "options": {"0": {"text": "off", "color": QUIET}, "1": {"text": "on", "color": "blue"}}}]


def _auto(meta, used: set) -> list:
    """Every recorded entity not on a fixed panel: numbers by device class / unit, on/off by domain."""
    groups: dict[tuple, list] = {}
    for e in meta:
        if e["id"] in used or "value" not in e.get("fields", ("value",)):
            continue
        if e["domain"] in NUMERIC or e.get("unit"):
            key = ("n", e.get("dc") or "", e.get("unit") or "")
        else:
            key = ("b", e["domain"], e.get("dc") or "")
        groups.setdefault(key, []).append(e)
    out = []
    for (kind, a, b), ents in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "~", kv[0][2])):
        ents.sort(key=lambda e: (e.get("name") or e["id"]).lower())
        parts = _chunks(ents)
        for i, part in enumerate(parts):
            if kind == "n":
                what = (a.replace("_", " ").capitalize() if a else "Other numbers") + (f" ({b})" if b else "")
                out.append(ts(_title(what, i, len(parts)), [(hq(part), "{{name}}")], UNITS.get(b, "short"), w=12))
            else:
                what = a.replace("_", " ").capitalize() + (f" · {b.replace('_', ' ')}" if b else "")
                out.append(timeline(_title(what, i, len(parts)), [(hq(part), "{{name}}")], w=12,
                                    h=max(4, 2 + len(part)), mappings=ONOFF))
    return out


def _tidy(name: str) -> str:
    """A friendlier label: Home Assistant names often end in the raw entity id ("Sh Basement Door
    basement_door_temperature"); underscores become spaces and words said twice are dropped
    ("Sh Basement Door temperature")."""
    if "_" not in name and len(set(name.casefold().split())) == len(name.split()):
        return name
    seen, out = set(), []
    for word in name.replace("_", " ").split():
        if word.casefold() not in seen:
            seen.add(word.casefold())
            out.append(word)
    return " ".join(out) or name


def _graph(title, ents, unit, calcs=("lastNotNull", "min", "max"), metric="value", inner="{sel}", suffix="", **kw):
    """The Home Assistant dashboard's graph style: 2 px lines, no fill, now/min/max in the legend."""
    return ts(title, [(hq(ents, metric, inner) + suffix, "{{name}}")], unit, lw=2, fill=0, calcs=calcs, **kw)


def _graphs(title, ents, fallback, **kw):
    """One graph per unit, at most PER_PANEL lines each (see _by_unit)."""
    groups, out = _units(ents), []
    for unit, part_all in groups:
        name = title + (f" ({unit})" if len(groups) > 1 and unit else "")
        parts = _chunks(part_all)
        for i, part in enumerate(parts):
            out.append(_graph(_title(name, i, len(parts)), part, UNITS.get(unit, fallback if len(groups) == 1 else "short"),
                              **kw))
    return out


def _pairs(panels):
    """Graphs side by side, each pair as tall as its longer legend; a lone last one takes the full width."""
    if len(panels) % 2:
        panels[-1]["_w"] = 24
    for i in range(0, len(panels), 2):
        pair = panels[i:i + 2]
        lines = max(len(p.get("targets", [])) if p["type"] != "timeseries" else _lines(p) for p in pair)
        h = max(p["_h"] for p in pair)
        if any(p["type"] == "timeseries" for p in pair):
            h = max(h, 6 + lines)  # the legend: one line per series under the graph
        for p in pair:
            p["_h"] = h
    return panels


def _lines(panel) -> int:
    """How many series a panel's queries name (one per entity id in the query)."""
    found = [m.count("|") + 1 for t in panel["targets"] for m in re.findall(r'entity_id=~"([^"]*)"', t["expr"])]
    return max(1, sum(found) or len(panel["targets"]))


def homeassistant(meta: list[dict] | None = None) -> dict:
    meta = [e | {"name": _tidy(e.get("name") or e["id"])} for e in meta or []]
    meta = sorted(unique_names(meta), key=lambda e: (e.get("name") or e["id"]).lower())
    used: set = set()

    def take(ents):
        used.update(e["id"] for e in ents)
        return ents

    temps, hum = take(_pick(meta, "sensor", "temperature")), take(_pick(meta, "sensor", "humidity"))
    press = take(_pick(meta, "sensor", {"pressure", "atmospheric_pressure"}))
    now_t = _pick(meta, "climate", field="current_temperature")[:LONG]
    target, active = _pick(meta, "climate", field="target_temperature")[:LONG], _pick(meta, "climate", field="active")[:LONG]
    take(now_t + target + active)
    # plugs often leave out the device class: their unit says it too
    power = take([e for e in _pick(meta) if e.get("dc") == "power" or e.get("unit") in ("W", "kW")][:LONG])
    energy = take([e for e in _pick(meta) if e.get("dc") == "energy" or e.get("unit") in ("Wh", "kWh")][:LONG])
    doors = take(_pick(meta, "binary_sensor", DOORS)[:LONG])
    bats = take(_pick(meta, "sensor", "battery")[:LONG])
    alarm, safety = take(_pick(meta, "alarm_control_panel")[:LONG]), take(_pick(meta, "binary_sensor", SAFETY_DC)[:LONG])
    lights = take(_pick(meta, "light"))
    host = [(f'hw_check_value_value{{{S},check="homeassistant.{c}"}}', n) for c, n in
            (("cpu", "CPU"), ("memory", "memory"), ("disk", "disk"))]
    quiet = [(None, "text")]

    # ---- now: one line of tiles
    tiles = []
    if temps:
        tiles.append(stat("Temperatures now", hq(temps[:8]), "celsius", "{{name}}", w=10, decimals=1,
                          text_mode="value_and_name", color_mode="value", steps=quiet))
    if alarm:
        tiles.append(stat("Alarm", hq(alarm), legend="{{name}}", w=5, text_mode="value_and_name", color_mode="value",
                          mappings=[{"type": "value", "options": {"0": {"text": "Disarmed", "color": "text"},
                                                                  "1": {"text": "Armed", "color": "blue"}}}]))
    if doors:
        tiles.append(stat("Doors open", f"sum({hq(doors)}) or vector(0)", w=3, decimals=0,
                          color_mode="value", steps=[(None, "green"), (1, "orange")]))
    if bats:
        tiles.append(stat("Low battery", f"count(({hq(bats)} >= 0) < 20) or vector(0)", w=3,
                          decimals=0, color_mode="value", steps=[(None, "green"), (1, "orange")],
                          desc="Batteries under 20 %. The Batteries graph below names them."))
    tiles.append(stat("HA CPU", host[0][0], "percent", w=max(3, 24 - sum(t["_w"] for t in tiles)) if len(tiles) > 1 else 4,
                      decimals=0, color_mode="value", steps=[(None, "text"), (70, "orange"), (90, "red")]))
    p = list(tiles)

    climate = _graphs("Temperatures", temps, "celsius") + _graphs("Humidity", hum, "percent", maximum=100)
    if now_t or target:
        climate.append(ts("Thermostats", ([(hq(now_t, "current_temperature"), "{{name}} now")] if now_t else [])
                          + ([(hq(target, "target_temperature"), "{{name}} target")] if target else []), "celsius",
                          lw=2, fill=0, calcs=("lastNotNull", "min", "max")))
    if active:
        climate.append(_graph("Heating hours per day", active, "h", calcs=("lastNotNull", "max"), minimum=0, maximum=24,
                              metric="active", inner="avg_over_time({sel}[1d]) * 24",
                              desc="Hours the thermostat was actually heating (or cooling) in the 24 h before each point."))
    if climate:
        p += [row("Climate")] + _pairs(climate)
    if active:
        p.append(timeline("Heating", [(hq(active, "active"), "{{name}}")], h=2 + len(active), mappings=[{
            "type": "value", "options": {"0": {"text": "idle", "color": QUIET}, "1": {"text": "heating", "color": "orange"}}}]))

    energy_p = (_graphs("Power", power, "watt", calcs=("lastNotNull", "mean", "max"), minimum=0)
                + _graphs("Energy per day", energy, "kwatth", calcs=("lastNotNull", "max"), inner="increase({sel}[1d])",
                          suffix=" > 0", minimum=0,
                          desc="Counters that restart (some plugs do at midnight or at a restart) are handled by increase()."))
    if energy_p:
        p += [row("Energy")] + _pairs(energy_p)

    sec = []
    if alarm:
        sec.append(timeline("Alarm", [(hq(alarm), "{{name}}")], w=12, h=7, mappings=[{"type": "value", "options": {
            "0": {"text": "disarmed", "color": QUIET}, "1": {"text": "armed", "color": "blue"}}}]))
    if doors:
        sec.append(timeline("Doors", [(hq(doors), "{{name}}")], w=12, h=7, mappings=[{"type": "value", "options": {
            "0": {"text": "closed", "color": QUIET}, "1": {"text": "open", "color": "orange"}}}]))
    if safety:
        sec.append(timeline("Safety sensors", [(hq(safety), "{{name}}")], w=12, h=7, mappings=[{"type": "value", "options": {
            "0": {"text": "clear", "color": QUIET}, "1": {"text": "ALARM", "color": "red"}}}]))
    if bats:
        sec.append(_graph("Batteries", bats, "percent", calcs=("lastNotNull", "min"), suffix=" >= 0",
                          minimum=0, maximum=100, h=7))
    if sec:
        p += [row("Security")] + _pairs(sec)

    p += [row("Home Assistant host"),
          ts("CPU, memory and disk", host, "percent", w=24, h=7, minimum=0, maximum=100, lw=2, fill=0,
             calcs=("lastNotNull", "mean", "max"), desc="From the host sensors in the module settings.")]

    # ---- the long parts, folded until opened
    if lights:
        p.append(row(f"Lights ({len(lights)})", collapsed=True))
        parts = _chunks(lights, LONG)
        for i, part in enumerate(parts):
            p.append(_graph(_title("Hours on per day", i, len(parts)), part, "h", calcs=("lastNotNull", "max"),
                            inner="avg_over_time({sel}[1d]) * 24", suffix=" > 0", w=12, h=8, minimum=0,
                            desc="Hours each light was on in the 24 h before each point."))
            p.append(timeline(_title("On and off", i, len(parts)), [(hq(part), "{{name}}")], w=12, h=8,
                              mappings=[{"type": "value", "options": {"0": {"text": "off", "color": QUIET},
                                                                      "1": {"text": "on", "color": "yellow"}}}]))
    if press:
        p += [row("Pressure", collapsed=True)] + _graphs("Pressure", press, "pressurehpa", w=24)
    auto = _auto(meta, used)
    if auto:
        for a in auto:
            if a["type"] == "timeseries":
                a["fieldConfig"]["defaults"]["custom"] |= {"lineWidth": 2, "fillOpacity": 0}
                a["options"]["legend"]["calcs"] = ["lastNotNull", "min", "max"]
        p += [row(f"Other recorded entities ({sum(1 for e in meta if e['id'] not in used)})", collapsed=True)] + _pairs(auto)
    if meta:
        # every entity in one query: names as value mappings, not in the query (VictoriaMetrics caps a query at 16 KB)
        # watched-only entities are stored too: list the dashboard's ones, 150 ids per query (16 KB)
        ids = ["|".join(e["id"].replace(".", "\\\\.") for e in meta[i:i + 150]) for i in range(0, len(meta), 150)]
        latest = table("Latest values", [f'label_copy(max by (entity_id) (hw_ha_entity_value{{{S},entity_id=~"{x}"}}), '
                                         f'"entity_id", "name")' for x in ids],
                       {"name": "Name", "entity_id": "Entity", "Value": "Now"}, h=12, hide=("domain",),
                       desc="Every recorded entity's latest number (on/off as 1/0). "
                            "The Entity explorer dashboard graphs any of them.")
        latest["fieldConfig"]["overrides"].append({"matcher": {"id": "byName", "options": "Name"}, "properties": [
            {"id": "mappings", "value": [{"type": "value", "options": {
                e["id"]: {"text": e.get("name") or e["id"], "index": i} for i, e in enumerate(meta)}}]}]})
        p += [row(f"Every recorded entity ({len(meta)})", collapsed=True), latest]
    else:
        p = [{"type": "text", "title": "", "_w": 24, "_h": 3, "options": {"mode": "markdown", "content": (
            "No entities shown yet. In homeWatch, open **Modules › Home Assistant**, and under **Entities** tick "
            "**Show in Grafana** for the ones you want here.")}}] + p
    return dashboard("hw-homeassistant", "Home Assistant", p,
                     "Built from the entities ticked Show in Grafana (Home Assistant module → Entities). Rebuilt when that list, "
                     "an entity's name or its unit changes.")


def _opt(text: str) -> str:
    return re.sub(r"[,:\\$]", " ", text).strip()  # the custom variable's syntax uses , and :


def ha_explorer(meta: list[dict] | None = None) -> dict:
    """Pick any recorded entities and see each on its own graph."""
    meta = [e | {"name": _tidy(e.get("name") or e["id"])} for e in meta or []]  # as on the Home Assistant dashboard
    meta = sorted([e for e in unique_names(meta) if "value" in e.get("fields", ("value",))],
                  key=lambda e: (e.get("name") or e["id"]).lower())
    opts = [(f"{_opt(e.get('name') or e['id'])}{' (' + _opt(e['unit']) + ')' if e.get('unit') else ''} · {e['id']}", e["id"])
            for e in meta]
    first = next((o for o in opts if o[1].startswith("sensor.") and "temperature" in o[1]), opts[0] if opts else None)
    panel = ts("${entity:text}", [(f'max by (entity_id) (hw_ha_entity_value{{{S},entity_id=~"$entity"}})', "value")],
               w=12, h=8, fill=20)
    panel |= {"repeat": "entity", "repeatDirection": "h", "maxPerRow": 2}
    panel["options"]["legend"]["showLegend"] = False  # one line, named in the title
    board = dashboard("hw-ha-explorer", "Home Assistant · Entity explorer", [panel],
                      "Choose entities (the ones shown in Grafana) at the top; each gets its own graph.", tags=("homeassistant",))
    board["templating"]["list"].append({
        "name": "entity", "label": "Entities", "type": "custom", "multi": True, "includeAll": False,
        "query": ", ".join(f"{t} : {v}" for t, v in opts),
        "options": [{"text": t, "value": v, "selected": first is not None and v == first[1]} for t, v in opts],
        "current": {"text": [first[0]], "value": [first[1]]} if first else {}})
    return board


BUILDERS = {"internet": internet, "speedport": speedport, "opnsense": opnsense, "proxmox": proxmox,
            "mikrotik": mikrotik, "unifi": unifi, "synology": synology, "pbs": pbs, "ups": ups, "docker": docker,
            "homeassistant": homeassistant}

WELCOME = dashboard("hw-my-welcome", "About My dashboards", [{
    "type": "text", "title": "", "_w": 24, "_h": 7, "options": {"mode": "markdown", "content": (
        "### Your own dashboards\n"
        "The dashboards in the **homeWatch** folder are rebuilt with every update, so they can't be saved. "
        "To change one, open it, choose **Save as** and save the copy here, in **My dashboards**. "
        "Copies here are yours: updates never touch them.\n\n"
        "Data source: **homewatch** (VictoriaMetrics, PromQL/MetricsQL). Metric names start with `hw_`; "
        "open **Explore** to browse them.")}}], "How to keep your own dashboards.")


# ---- provisioning files ---------------------------------------------------------------------------

def files(enabled: set[str], ha: list[dict] | None = None) -> dict[str, str]:
    """Relative path -> content of every provisioning file for this set of enabled modules."""
    ds = {"apiVersion": 1, "datasources": [{
        "name": "homewatch", "uid": DS["uid"], "type": "prometheus", "access": "proxy",
        "url": "http://victoriametrics:8428", "isDefault": True, "editable": False,
        # max_lookback: modules read every 10 or 15 minutes; without it a "now" value is missing between readings
        "jsonData": {"httpMethod": "POST", "timeInterval": "1m", "customQueryParameters": "max_lookback=20m"}}]}
    providers = {"apiVersion": 1, "providers": [
        {"name": "homewatch", "folder": FOLDER, "folderUid": "homewatch", "type": "file", "disableDeletion": True,
         "allowUiUpdates": False, "updateIntervalSeconds": 60,
         "options": {"path": "/etc/grafana/provisioning/dashboards/homewatch"}},
        {"name": "my", "folder": MY_FOLDER, "folderUid": "my-dashboards", "type": "file", "disableDeletion": True,
         "allowUiUpdates": False, "updateIntervalSeconds": 600,
         "options": {"path": "/etc/grafana/provisioning/dashboards/my"}}]}
    out = {"datasources/homewatch.yaml": json.dumps(ds, indent=1),  # JSON is valid YAML
           "dashboards/homewatch.yaml": json.dumps(providers, indent=1),
           "dashboards/my/welcome.json": json.dumps(WELCOME, indent=1),
           "dashboards/homewatch/overview.json": json.dumps(overview(enabled, ha), indent=1)}
    for mid in sorted(enabled):
        if mid == "homeassistant":
            out["dashboards/homewatch/homeassistant.json"] = json.dumps(homeassistant(ha), indent=1)
            out["dashboards/homewatch/ha-explorer.json"] = json.dumps(ha_explorer(ha), indent=1)
        elif mid in BUILDERS:
            out[f"dashboards/homewatch/{mid}.json"] = json.dumps(BUILDERS[mid](), indent=1)
    return out


def write(directory: str, enabled: set[str], ha: list[dict] | None = None) -> bool:
    """Bring the provisioning directory in line with the enabled modules; True when anything changed."""
    if not directory or not os.path.isdir(directory):
        return False
    want = files(enabled, ha)
    changed = False
    for rel, text in want.items():
        path = os.path.join(directory, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    continue
        except FileNotFoundError:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        changed = True
    board_dir = os.path.join(directory, "dashboards", "homewatch")
    for name in os.listdir(board_dir):  # a switched-off module's dashboard goes
        if f"dashboards/homewatch/{name}" not in want:
            os.remove(os.path.join(board_dir, name))
            changed = True
    return changed


# ---- talking to Grafana (status, nightly copy of "My dashboards") --------------------------------

# homewatch reads Grafana as this Viewer: it shares the network Grafana trusts for the user header
EXPORTER = {"X-Webauth-User": "homewatch-backup", "X-Webauth-Role": "Viewer"}
MY_EXPORT = re.compile(r"^grafana-my-dashboards-\d{4}-\d{2}-\d{2}\.json$")


async def running(http, base: str) -> bool:
    try:
        r = await http.get(f"{base}/api/health", timeout=3)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 - not installed, stopped or starting: all the same here
        return False


async def export_my(http, base: str, folder: str, today: str, keep: int = 14) -> str:
    """Save the dashboards in "My dashboards" as one JSON file (Grafana's own format, one entry per dashboard,
    each importable with Dashboards → New → Import). Returns the file's path, or '' when Grafana isn't there."""
    if not await running(http, base):
        return ""
    r = await http.get(f"{base}/api/search", params={"folderUIDs": "my-dashboards", "type": "dash-db"},
                       headers=EXPORTER, timeout=15)
    r.raise_for_status()
    boards = []
    for hit in r.json():
        if hit.get("uid") == WELCOME["uid"]:
            continue
        d = await http.get(f"{base}/api/dashboards/uid/{hit['uid']}", headers=EXPORTER, timeout=15)
        d.raise_for_status()
        boards.append(d.json().get("dashboard"))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"grafana-my-dashboards-{today}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"saved": today, "folder": MY_FOLDER, "dashboards": boards}, f, indent=1)
    os.replace(path + ".tmp", path)
    for old in sorted((n for n in os.listdir(folder) if MY_EXPORT.match(n)), reverse=True)[keep:]:
        os.remove(os.path.join(folder, old))
    return path
