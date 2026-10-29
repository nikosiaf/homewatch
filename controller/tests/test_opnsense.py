"""OPNsense checks and the shared API-module flow (connect, choices, switch off, remove)."""

import time

import pytest

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.apibase import ApiError
from homewatch.modules.opnsense.plugin import plugin

NOW = time.time()


def snap(**over):
    s = {
        "gateways": [{"name": "WAN", "status": "none", "status_translated": "Online", "loss": "0.0 %", "delay": "9 ms"}],
        "resources": {"memory": {"total": "8000", "used": "5000", "arc": "1000"}},
        "disk": {"devices": [{"mountpoint": "/", "used_pct": 3, "used": "2G", "total": "60G"}]},
        "time": {"uptime": "1 day", "loadavg": "0.3, 0.3, 0.3"},
        "activity": {"headers": ["CPU:  2.9% user,  0.0% nice,  1.8% system,  0.1% interrupt, 95.1% idle"]},
        "services": [{"id": "unbound", "description": "Unbound DNS", "running": 1},
                     {"id": "haproxy", "description": "HAProxy", "running": 1}],
        "haproxy": [{"pxname": "web", "svname": "FRONTEND", "status": "OPEN"},
                    {"pxname": "nas", "svname": "nas", "status": "UP", "check_status": "L4OK"},
                    {"pxname": "old", "svname": "old", "status": "no check", "check_status": ""}],
        "certs": [{"descr": "acme", "valid_to": str(NOW + 80 * 86400), "in_use": "1"},
                  {"descr": "unused", "valid_to": str(NOW - 86400), "in_use": "0"}],
        "firmware": {"status": "none", "needs_reboot": "0", "product_version": "26.7.7", "upgrade_packages": [],
                     "new_packages": [], "last_check": "today"},
        "info": {"name": "fw", "versions": ["OPNsense 26.7.7"]},
        "_summary": "fw 26.7.7",
    }
    s.update(over)
    return s


def th():
    return {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}


def run(sn, chosen=("unbound", "haproxy"), error=""):
    s = {"enabled": True, "chosen": list(chosen)}
    return {r.key.split(".")[1]: r for r in plugin.evaluate(s, th(), (s, sn, error, NOW))}


def test_healthy():
    res = run(snap())
    assert all(r.status == OK for r in res.values()), {k: r.detail for k, r in res.items()}
    assert res["cpu"].value == pytest.approx(4.9) and res["mem"].value == pytest.approx(50)
    assert "acme expires in 80 days" in res["certs"].detail  # the expired cert is not in use


def test_problems():
    res = run(snap(gateways=[{"name": "WAN", "status": "down", "status_translated": "Offline"}],
                   services=[{"id": "unbound", "description": "Unbound DNS", "running": 0}],
                   haproxy=[{"pxname": "nas", "svname": "nas", "status": "DOWN"}],
                   certs=[{"descr": "acme", "valid_to": str(NOW + 5 * 86400), "in_use": "1"}],
                   firmware={"status": "update", "needs_reboot": "0", "upgrade_packages": [1, 2],
                             "product_version": "26.7.6"}))
    assert res["gateways"].status == ERROR and res["services"].status == ERROR
    assert res["haproxy"].status == ERROR and "nas" in res["haproxy"].detail
    assert res["certs"].status == ERROR and res["firmware"].status == WARNING


def test_unreadable_suppresses_the_rest():
    res = run(None, error="OPNsense rejected the API key")
    assert res["api"].status == ERROR and res["gateways"].suppressed_by == "opnsense.api"


def test_missing_privileges_are_not_alarms():
    res = run(snap(activity=None, services=None, haproxy=None, certs=None, firmware=None))
    assert all(r.status == OK for r in res.values())
    assert "Diagnostics: System Activity" in res["cpu"].detail


async def test_wrong_port_says_where_the_gui_is():
    import httpx
    import pytest
    import respx

    from homewatch.modules.apibase import ApiError
    s = {"url": "https://fw.test", "api_key": "k", "verify": False}
    with respx.mock:
        # HAProxy (or another site) on 443: a 503 page, or an HTML page that is not the API
        respx.get(url__startswith="https://fw.test/api/").mock(return_value=httpx.Response(
            503, text="<h1>503 Service Unavailable</h1>", headers={"content-type": "text/html"}))
        with pytest.raises(ApiError, match="not like the OPNsense API.*TCP port"):
            await plugin.fetch(s, "secret", 0)
    with respx.mock:
        respx.get(url__startswith="https://fw.test/api/").mock(return_value=httpx.Response(
            200, text="<html>login</html>", headers={"content-type": "text/html"}))
        with pytest.raises(ApiError, match="web GUI with its port"):
            await plugin.fetch(s, "secret", 0)


async def test_missing_privilege_is_named():
    import httpx
    import pytest
    import respx

    s = {"url": "https://fw.test", "api_key": "k", "verify": False}
    with respx.mock:
        respx.get("https://fw.test/api/routes/gateway/status").mock(return_value=httpx.Response(200, json={"items": []}))
        respx.get(url__startswith="https://fw.test/api/diagnostics/system/").mock(return_value=httpx.Response(403, json={}))
        with pytest.raises(ApiError, match="lacks the privilege Lobby: Dashboard"):
            await plugin.fetch(s, "secret", 0)
