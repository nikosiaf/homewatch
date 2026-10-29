"""PBS: token header, groups per datastore, job results, checks (simulated API: no live PBS access)."""

import time

import httpx
import pytest
import respx

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.apibase import ApiError
from homewatch.modules.pbs.plugin import plugin

NOW = time.time()
BASE = "https://pbs.test:8007/api2/json"
S = {"url": "https://pbs.test:8007", "token_id": "homewatch@pbs!monitor", "verify": False, "enabled": True}


def mock(tasks=None, store_error=None):
    respx.get(BASE + "/version").mock(return_value=httpx.Response(200, json={"data": {"version": "4.1"}}))
    store = {"store": "main", "total": 1000, "used": 400, "avail": 600, "estimated-full-date": 0}
    if store_error:
        store["error"] = store_error
    respx.get(BASE + "/status/datastore-usage").mock(return_value=httpx.Response(200, json={"data": [store]}))
    respx.get(BASE + "/admin/datastore/main/groups").mock(return_value=httpx.Response(200, json={"data": [
        {"backup-type": "vm", "backup-id": 100, "last-backup": NOW - 5 * 3600, "backup-count": 7},
        {"backup-type": "ct", "backup-id": 104, "last-backup": NOW - 60 * 3600, "backup-count": 7},
        {"backup-type": "vm", "backup-id": 999, "last-backup": NOW - 30 * 86400, "backup-count": 1}]}))
    respx.get(BASE + "/admin/datastore/main/snapshots").mock(return_value=httpx.Response(200, json={"data": [
        {"backup-type": "vm", "backup-id": "100", "backup-time": NOW - 5 * 3600, "comment": "router"},
        {"backup-type": "vm", "backup-id": "100", "backup-time": NOW - 29 * 3600, "comment": "old name"},
        {"backup-type": "ct", "backup-id": "104", "backup-time": NOW - 60 * 3600, "comment": "dns\nsecond line"},
        {"backup-type": "vm", "backup-id": "999", "backup-time": NOW - 30 * 86400}]}))
    respx.get(url__startswith=BASE + "/nodes/localhost/tasks").mock(return_value=httpx.Response(200, json={"data": tasks or [
        {"worker_type": "garbage_collection", "worker_id": "main", "endtime": NOW - 3600, "status": "OK"},
        {"worker_type": "verificationjob", "worker_id": "main:v1", "endtime": NOW - 7200, "status": "OK"},
        {"worker_type": "backup", "worker_id": "main:vm/100", "endtime": NOW - 5 * 3600, "status": "OK"}]}))
    respx.get(BASE + "/nodes/localhost/apt/update").mock(return_value=httpx.Response(200, json={"data": []}))


def run(snap, chosen):
    s = S | {"chosen": chosen}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    return {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, snap, "", NOW))}


@respx.mock
async def test_fetch_and_default_groups():
    mock()
    snap = await plugin.fetch(S, "SECRET", NOW)
    assert respx.calls.last.request.headers["authorization"] == "PBSAPIToken=homewatch@pbs!monitor:SECRET"
    # only guests backed up in the last 26 h are ticked: 104 (60 h) and 999 (a month) look left out of the job
    assert plugin.default_chosen(snap) == ["main/vm/100"]
    # names come from the newest snapshot's comment; one datastore: no datastore detail
    assert [(c.label, c.detail) for c in plugin.choices(snap)] == [
        ("VM 100 · router", ""), ("VM 999", ""), ("Container 104 · dns", "")]
    res = run(snap, ["main/vm/100"])
    assert all(r.status == OK for r in res.values()), {k: r.detail for k, r in res.items()}
    res = run(snap, ["main/vm/100", "main/ct/104"])
    assert res["backups"].status == ERROR and "dns (CT 104) 60 h old" in res["backups"].detail


@respx.mock
async def test_failed_jobs_and_broken_datastore():
    mock(tasks=[{"worker_type": "verificationjob", "worker_id": "main:v1", "endtime": NOW - 7200,
                 "status": "verification failed - 2 chunks bad"},
                {"worker_type": "garbage_collection", "worker_id": "main", "endtime": NOW - 100,
                 "status": "WARNINGS: 1"}], store_error="datastore not mounted")
    snap = await plugin.fetch(S, "s", NOW)
    res = run(snap, [])
    assert res["jobs"].status == ERROR and "verify main:v1" in res["jobs"].detail
    assert res["datastores"].status == ERROR and "not mounted" in res["datastores"].detail


@respx.mock
async def test_rejected_token():
    respx.get(BASE + "/version").mock(return_value=httpx.Response(401))
    with pytest.raises(ApiError, match="rejected"):
        await plugin.fetch(S, "bad", NOW)
