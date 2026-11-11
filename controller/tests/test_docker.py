"""Docker hosts module: read-only proxy API, stopped containers, crash loops, health, removals."""

import asyncio
import time

import httpx

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.docker.plugin import DockerModule, hosts

NOW = time.time()
REAL = httpx.AsyncClient


def ctr(cid, name, state="running", policy="unless-stopped", restarts=0, health=None, project="stack", exit_code=0):
    st = {"Status": state, "ExitCode": exit_code, "StartedAt": "2026-11-04T08:00:00Z"}
    if health:
        st["Health"] = {"Status": health}
    return {"Id": cid * 8, "Name": "/" + name, "State": st, "RestartCount": restarts,
            "HostConfig": {"RestartPolicy": {"Name": policy}},
            "Config": {"Image": f"img/{name}:1", "Labels": {"com.docker.compose.project": project} if project else {}}}


def fake(monkeypatch, per_host, down=()):
    calls = []

    def handler(request):
        host = f"{request.url.host}:{request.url.port}"
        calls.append((host, request.method, request.url.path))
        if host in down:
            raise httpx.ConnectError("refused")
        cs = per_host[host]
        path = request.url.path
        if path == "/info":
            return httpx.Response(200, json={"Name": host.split(":")[0].replace(".", "-"), "ServerVersion": "29.0"})
        if path == "/containers/json":
            return httpx.Response(200, json=[{"Id": c["Id"], "Image": c["Config"]["Image"]} for c in cs])
        for c in cs:
            if path == f"/containers/{c['Id']}/json":
                return httpx.Response(200, json=c)
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL(transport=httpx.MockTransport(handler), **kw))
    return calls


def evaluate(m, s, snap, now=NOW):
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in m.checks}
    return {r.key.split(".")[1]: r for r in m.evaluate(s, th, (s, snap, "", now))}


def test_hosts_and_validation():
    assert hosts("10.0.0.5, dock.lan:2400") == ["10.0.0.5:2375", "dock.lan:2400"]
    m = DockerModule()
    out, err = m.validate({"hosts": " 10.0.0.5  dock.lan:2400 "}, {})
    assert not err and out["hosts"] == "10.0.0.5, dock.lan:2400"
    assert "Docker hosts" in m.validate({"hosts": "http://x"}, {})[1]
    assert "Docker hosts" in m.validate({"hosts": ""}, {})[1]


def test_read_only_and_healthy(monkeypatch):
    calls = fake(monkeypatch, {"10.0.0.5:2375": [ctr("a", "web", health="healthy"), ctr("b", "db"),
                                                 ctr("c", "oneshot", state="exited", policy="no")]})
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5"}
    snap = asyncio.run(m.fetch(s, "", NOW))
    assert all(method == "GET" for _, method, _ in calls)
    assert [c["name"] for c in snap["hosts"][0]["containers"]] == ["db", "oneshot", "web"]
    res = evaluate(m, s, snap)
    assert all(r.status == OK for r in res.values())
    assert res["stopped"].detail == "2 watched containers running"  # 'oneshot' has no restart policy
    assert res["unhealthy"].detail == "1 health checks passing"
    assert [c.value for c in m.filters(snap)] == ["10.0.0.5:2375/db", "10.0.0.5:2375/web"]


def test_stopped_unhealthy_and_skipped(monkeypatch):
    fake(monkeypatch, {"10.0.0.5:2375": [ctr("a", "web", state="exited", exit_code=137),
                                         ctr("b", "db", health="unhealthy"),
                                         ctr("c", "toy", state="exited", project="lab")]})
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5", "skipped": ["group:10-0-0-5 · lab"]}
    res = evaluate(m, s, asyncio.run(m.fetch(s, "", NOW)))
    assert res["stopped"].status == ERROR and res["stopped"].detail == "not running: web (exited, exit 137)"
    assert res["unhealthy"].status == WARNING and "db" in res["unhealthy"].detail


def test_crash_loop_counted_over_the_hour(monkeypatch):
    per = {"10.0.0.5:2375": [ctr("a", "web", restarts=5)]}
    fake(monkeypatch, per)
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5"}
    assert evaluate(m, s, asyncio.run(m.fetch(s, "", NOW)))["restarts"].status == OK  # old restarts don't count
    per["10.0.0.5:2375"] = [ctr("a", "web", restarts=6)]
    r = evaluate(m, s, asyncio.run(m.fetch(s, "", NOW + 120)), NOW + 120)["restarts"]
    assert r.status == WARNING and r.value == 1
    per["10.0.0.5:2375"] = [ctr("a", "web", restarts=8, state="restarting")]
    r = evaluate(m, s, asyncio.run(m.fetch(s, "", NOW + 240)), NOW + 240)
    assert r["restarts"].status == ERROR and r["stopped"].status == OK  # restarting is a loop, not stopped
    r = evaluate(m, s, asyncio.run(m.fetch(s, "", NOW + 2 * 3600)), NOW + 2 * 3600)["restarts"]
    assert r.status == OK
    per["10.0.0.5:2375"] = [ctr("a", "web", restarts=0)]  # recreated: counter starts again
    assert evaluate(m, s, asyncio.run(m.fetch(s, "", NOW + 3 * 3600)), NOW + 3 * 3600)["restarts"].status == OK


def test_removed_container_until_page_saved(monkeypatch):
    per = {"10.0.0.5:2375": [ctr("a", "web"), ctr("b", "db")], "10.0.0.6:2375": [ctr("c", "nvr")]}
    fake(monkeypatch, per, down=())
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5, 10.0.0.6"}
    evaluate(m, s, asyncio.run(m.fetch(s, "", NOW)))
    per["10.0.0.5:2375"] = [ctr("a", "web")]
    res = evaluate(m, s, asyncio.run(m.fetch(s, "", NOW + 60)))
    assert res["removed"].status == WARNING and res["removed"].detail == "removed: 10-0-0-5/db"
    assert evaluate(m, s, asyncio.run(m.fetch(s, "", NOW + 120)))["removed"].status == WARNING  # stays
    m.seen = {}  # what saving the page does
    assert evaluate(m, s, asyncio.run(m.fetch(s, "", NOW + 180)))["removed"].status == OK


def test_one_host_down(monkeypatch):
    fake(monkeypatch, {"10.0.0.5:2375": [ctr("a", "web")], "10.0.0.6:2375": [ctr("c", "nvr")]})
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5, 10.0.0.6"}
    evaluate(m, s, asyncio.run(m.fetch(s, "", NOW)))
    fake(monkeypatch, {"10.0.0.5:2375": [ctr("a", "web")], "10.0.0.6:2375": []}, down=("10.0.0.6:2375",))
    res = evaluate(m, s, asyncio.run(m.fetch(s, "", NOW + 60)))
    assert res["each"].status == ERROR and "10.0.0.6:2375" in res["each"].detail
    assert res["removed"].status == OK  # an unreadable host's containers are not 'removed'
    fake(monkeypatch, {"10.0.0.5:2375": []}, down=("10.0.0.5:2375",))
    s1 = {"enabled": True, "hosts": "10.0.0.5"}
    try:
        asyncio.run(m.fetch(s1, "", NOW))
        raise AssertionError("expected ApiError")
    except Exception as e:  # noqa: BLE001
        assert "can't reach the Docker proxy" in str(e)


def test_proxy_refusing_containers(monkeypatch):
    def handler(request):
        return httpx.Response(403) if request.url.path.startswith("/containers") else httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL(transport=httpx.MockTransport(handler), **kw))
    try:
        asyncio.run(DockerModule().fetch({"hosts": "10.0.0.5"}, "", NOW))
        raise AssertionError("expected ApiError")
    except Exception as e:  # noqa: BLE001
        assert "CONTAINERS=1" in str(e)


def test_list_is_grouped_per_host_and_old_project_skips_carry_over(monkeypatch, db):
    from types import SimpleNamespace
    fake(monkeypatch, {"10.0.0.5:2375": [ctr("a", "web"), ctr("b", "db"), ctr("c", "toy", project="lab")]})
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5"}
    m.snap = asyncio.run(m.fetch(s, "", NOW))
    fs = m.filters(m.snap)
    assert {c.group for c in fs} == {"10-0-0-5"}  # one group per host, not per compose project
    assert next(c for c in fs if c.label == "toy").detail == "lab"  # state only when not running
    rt = SimpleNamespace(db=db)
    db.set("module.docker", s | {"skipped": ["group:10-0-0-5 · lab"]})  # saved by 0.27
    assert m.load(rt)["skipped"] == ["10.0.0.5:2375/toy"]
    res = evaluate(m, m.load(rt), m.snap)
    assert res["stopped"].detail == "2 watched containers running"
    db.set("module.docker", s | {"skipped": ["group:10-0-0-5"]})  # a whole host unticked
    assert evaluate(m, m.load(rt), m.snap)["stopped"].detail == "0 watched containers running"


def test_container_tables_line_up_across_hosts(monkeypatch):
    fake(monkeypatch, {"10.0.0.5:2375": [ctr("a", "web")], "10.0.0.6:2375": [ctr("b", "a-much-longer-container-name")]})
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5, 10.0.0.6"}
    tables = m.tables(asyncio.run(m.fetch(s, "", NOW)), s)
    assert len(tables) == 2 and tables[0].widths == tables[1].widths and len(tables[0].widths) == len(tables[0].head)
    assert tables[0].min_width >= 640


def test_pinned_digest_is_shortened_in_the_table():
    from homewatch.modules.docker.plugin import short_image
    assert short_image("docker.io/valkey/valkey:8@sha256:81db6d39e1bba3b3ff32bd3a1b19a6d69690f94a") == \
        "docker.io/valkey/valkey:8@sha256:81db6d39e1bb"
    assert short_image("caddy:2.11.7") == "caddy:2.11.7" and short_image("") == ""


METRICS = """# HELP apt_upgrades_pending Apt packages pending updates by origin.
# TYPE apt_upgrades_pending gauge
apt_upgrades_pending{arch="amd64",origin="Ubuntu:24.04/noble-updates"} 25
apt_upgrades_pending{arch="all",origin="Ubuntu:24.04/noble-security"} 4
node_reboot_required 1
node_uname_info{domainname="(none)",machine="x86_64",nodename="ubuntults",release="6.8.0-147-generic"} 1
"""


def fake_with_exporters(monkeypatch, metrics):
    """metrics: exporter address -> /metrics text, or None = unreachable."""
    calls = []

    def handler(request):
        host = f"{request.url.host}:{request.url.port}"
        calls.append((host, request.method, request.url.path))
        if host in metrics:
            if metrics[host] is None:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, text=metrics[host])
        if request.url.path == "/info":
            return httpx.Response(200, json={"Name": "dock", "ServerVersion": "29.0"})
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL(transport=httpx.MockTransport(handler), **kw))
    return calls


def test_parse_apt():
    from homewatch.modules.docker.plugin import parse_apt
    assert parse_apt(METRICS) == (29, True, "ubuntults")
    assert parse_apt('apt_upgrades_pending{arch="",origin=""} 0\nnode_reboot_required 0\n') == (0, False, "")
    assert parse_apt("node_load1 0.5\n") == (None, False, "")  # no apt collector
    # nothing pending: the collector writes no apt_upgrades_pending at all; held (phased) ones are not counted
    held = ('apt_autoremove_pending 7\napt_upgrades_held{arch="all",origin="Ubuntu:noble/noble-updates"} 1\n'
            'node_reboot_required 0\n')
    assert parse_apt(held) == (0, False, "")


def test_exporter_validation():
    m = DockerModule()
    out, err = m.validate({"hosts": "socket-proxy", "exporters": " 10.0.0.5  10.0.0.6:9200 "}, {})
    assert not err and out["exporters"] == "10.0.0.5, 10.0.0.6:9200"
    assert "node_exporter" in m.validate({"hosts": "socket-proxy", "exporters": "http://x"}, {})[1]
    assert not m.validate({"hosts": "socket-proxy"}, {})[1]  # optional


def test_host_updates_todo(monkeypatch):
    calls = fake_with_exporters(monkeypatch, {"10.0.0.5:9100": METRICS,
                                              "10.0.0.6:9100": 'apt_upgrades_pending{arch="",origin=""} 0\n'})
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5", "exporters": "10.0.0.5, 10.0.0.6"}
    snap = asyncio.run(m.fetch(s, "", NOW))
    assert all(method == "GET" for _, method, _ in calls)
    res = evaluate(m, s, snap)
    assert res["updates"].status == WARNING  # todo.apply turns this into a To do row
    assert res["updates"].detail == "ubuntults: 29 package updates, reboot needed"
    assert res["each"].status == OK
    m.snap, m.snap_ts = snap, NOW
    host = [x for x in m.series("home") if x.startswith("hw_docker_host,")]
    assert len(host) == 2 and "host=ubuntults" in host[0] and "updates=29" in host[0] and "reboot_required=1" in host[0]
    assert any(t.title.startswith("Host updates") for t in m.tables(snap, s))


def test_host_updates_off_and_problems(monkeypatch):
    fake_with_exporters(monkeypatch, {"10.0.0.5:9100": "node_load1 0.1\n", "10.0.0.6:9100": None})
    m = DockerModule()
    s = {"enabled": True, "hosts": "10.0.0.5"}
    res = evaluate(m, s, asyncio.run(m.fetch(s, "", NOW)))
    assert res["updates"].status == OK and "not checked" in res["updates"].detail
    s = {"enabled": True, "hosts": "10.0.0.5", "exporters": "10.0.0.5, 10.0.0.6"}
    res = evaluate(m, s, asyncio.run(m.fetch(s, "", NOW)))
    assert res["each"].status == ERROR
    assert "prometheus-node-exporter-collectors" in res["each"].detail and "can't reach node_exporter" in res["each"].detail
    assert res["updates"].status == OK and res["updates"].detail == "not readable"
