"""Proxmox: backup log parsing, checks, and the add-host flow against a mocked API."""

import httpx
import pytest
import respx

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.proxmox.checks import CHECKS, State, evaluate
from homewatch.modules.proxmox.client import Client, Snapshot, parse_vzdump_log

NOW = 1_800_000_000.0
LOG = [
    "INFO: Starting Backup of VM 100 (qemu)", "INFO: Finished Backup of VM 100 (00:14:56)",
    "INFO: Starting Backup of VM 103 (lxc)",
    "ERROR: Backup of VM 103 failed - error pruning backups - check log",
    "INFO: Starting Backup of VM 104 (lxc)",
]


def test_parse_vzdump_log():
    assert parse_vzdump_log(LOG) == {100: "ok", 103: "error pruning backups - check log", 104: "did not finish"}


def th(**over):
    t = {c.id: Thresholds(c.warn, c.error, c.default_on) for c in CHECKS}
    t.update(over)
    return t


def snap(**over):
    s = Snapshot(
        nodes=[{"node": "pve", "status": "online", "cpu": 0.08, "maxcpu": 16, "mem": 49e9, "maxmem": 67e9,
                "disk": 17e9, "maxdisk": 68e9, "uptime": 151817}],
        guests=[{"vmid": 100, "name": "OPNsense", "status": "running", "type": "qemu"},
                {"vmid": 103, "name": "adguard", "status": "stopped", "type": "lxc"},
                {"vmid": 104, "name": "unifi", "status": "running", "type": "lxc"}],
        storages=[{"storage": "pbs", "node": "pve", "status": "available", "disk": 167e9, "maxdisk": 4327e9},
                  {"storage": "zfsMirror", "node": "pve", "status": "available", "disk": 240e9, "maxdisk": 965e9}],
        disks={"pve": [{"devpath": "/dev/nvme0n1", "health": "PASSED", "wearout": 92},
                       {"devpath": "/dev/sda", "health": "PASSED", "wearout": "N/A"}]},
        backups={100: {"ok": NOW - 5 * 3600, "failed": None, "error": ""},
                 104: {"ok": NOW - 6 * 3600, "failed": None, "error": ""}},
        not_covered={103},
    )
    for k, v in over.items():
        setattr(s, k, v)
    return s


def by_id(results):
    return {r.key.split(".")[1]: r for r in results}


def test_healthy_host():
    res = by_id(evaluate({}, th(), State(NOW, snap(), watched=[100, 104])))
    assert all(r.status == OK for r in res.values()), {k: r.detail for k, r in res.items()}
    assert "lowest SSD life left 92 %" in res["disks"].detail
    assert "2 guests backed up" in res["backups"].detail


def test_api_down_suppresses_everything():
    res = by_id(evaluate({}, th(), State(NOW, None, "Proxmox rejected the API token", [100])))
    assert res["api"].status == ERROR and "rejected" in res["api"].detail
    assert res["guests"].suppressed_by == "proxmox.api"


def test_watched_guest_stopped():
    res = by_id(evaluate({}, th(), State(NOW, snap(), watched=[100, 103])))
    assert res["guests"].status == ERROR and "adguard (103) stopped" in res["guests"].detail
    assert "1 guests backed up" in res["backups"].detail  # 103 is in no backup job: not judged


def test_backup_failed_and_old():
    b = {100: {"ok": NOW - 30 * 3600, "failed": None, "error": ""},
         104: {"ok": NOW - 30 * 3600, "failed": NOW - 3600, "error": "error pruning backups - check log"}}
    res = by_id(evaluate({}, th(), State(NOW, snap(backups=b), watched=[100, 104])))
    assert res["backups"].status == ERROR
    assert "unifi: last backup failed (error pruning" in res["backups"].detail
    assert "OPNsense: newest backup 30 h old" in res["backups"].detail


def test_storage_disk_and_resources():
    st = snap(storages=[{"storage": "local", "node": "pve", "status": "available", "disk": 90, "maxdisk": 100}],
              disks={"pve": [{"devpath": "/dev/sdb", "health": "FAILED", "wearout": 50}]},
              nodes=[{"node": "pve", "status": "online", "cpu": 0.97, "mem": 92, "maxmem": 100, "disk": 1,
                      "maxdisk": 100}])
    res = by_id(evaluate({}, th(), State(NOW, st, watched=[])))
    assert res["storage"].status == WARNING and res["disks"].status == ERROR
    assert res["cpu"].status == ERROR and res["mem"].status == WARNING
    st2 = snap(disks={"pve": [{"devpath": "/dev/nvme0n1", "health": "PASSED", "wearout": 15}]})
    assert by_id(evaluate({}, th(), State(NOW, st2, watched=[])))["disks"].status == WARNING


def test_node_offline():
    st = snap(nodes=[{"node": "pve", "status": "offline"}])
    res = by_id(evaluate({}, th(), State(NOW, st, watched=[100])))
    assert res["node"].status == ERROR and res["backups"].suppressed_by == "proxmox.node"


BASE = "https://pve.test:8006/api2/json"


def mock_api():
    respx.get(BASE + "/nodes").mock(return_value=httpx.Response(200, json={"data": snap().nodes}))
    respx.get(BASE + "/cluster/resources", params={"type": "vm"}).mock(
        return_value=httpx.Response(200, json={"data": snap().guests}))
    respx.get(BASE + "/cluster/resources", params={"type": "storage"}).mock(
        return_value=httpx.Response(200, json={"data": snap().storages}))
    respx.get(BASE + "/cluster/backup-info/not-backed-up").mock(
        return_value=httpx.Response(200, json={"data": [{"vmid": 103}]}))
    respx.get(BASE + "/nodes/pve/disks/list").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(url__startswith=BASE + "/nodes/pve/tasks?").mock(return_value=httpx.Response(200, json={"data": [
        {"upid": "UPID:pve:1:2:3:vzdump::root@pam:", "id": "", "status": "job errors", "endtime": NOW - 3600},
        {"upid": "UPID:pve:4:5:6:vzdump:104:root@pam:", "id": "104", "status": "OK", "endtime": NOW - 7200}]}))
    log = respx.get(url__startswith=BASE + "/nodes/pve/tasks/UPID%3Apve%3A1").mock(
        return_value=httpx.Response(200, json={"data": [{"t": x} for x in LOG]}))
    return log


@respx.mock
async def test_client_snapshot_reads_each_log_once():
    log = mock_api()
    c = Client("https://pve.test:8006", "homewatch@pve!monitor", "secret")
    s = await c.snapshot(now=NOW)
    await c.snapshot(now=NOW)
    assert log.call_count == 1
    assert s.backups[100]["ok"] == NOW - 3600 and s.backups[103]["error"].startswith("error pruning")
    # 104: its own task succeeded earlier, the newer job never finished it -> counts as failed
    assert s.backups[104] == {"ok": NOW - 7200, "failed": NOW - 3600, "error": "did not finish"}
    assert s.not_covered == {103}
    headers = respx.calls.last.request.headers
    assert headers["authorization"] == "PVEAPIToken=homewatch@pve!monitor=secret"
    await c.close()


@respx.mock
async def test_rejected_token():
    respx.get(BASE + "/nodes").mock(return_value=httpx.Response(401))
    c = Client("https://pve.test:8006", "a@pve!b", "bad")
    with pytest.raises(Exception, match="rejected the API token"):
        await c.snapshot(now=NOW)
    await c.close()


def test_guest_without_any_backup_warns():
    res = by_id(evaluate({}, th(), State(NOW, snap(backups={100: {"ok": NOW - 3600, "failed": None, "error": ""}}),
                                         watched=[100, 104])))
    assert res["backups"].status == WARNING and "unifi: no backup in the last 8 days" in res["backups"].detail
