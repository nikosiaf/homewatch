"""UPS over NUT, against a tiny fake NUT server."""

import asyncio
import time

import pytest

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.apibase import ApiError
from homewatch.modules.ups.plugin import plugin

NOW = time.time()


async def fake_nut(variables: dict | None, stale: bool = False):
    async def handle(reader, writer):
        while line := (await reader.readline()).decode().strip():
            if line == "LIST UPS":
                writer.write(b'BEGIN LIST UPS\nUPS ups "Rack UPS"\nEND LIST UPS\n')
            elif line.startswith("LIST VAR"):
                if stale:
                    writer.write(b"ERR DATA-STALE\n")
                else:
                    body = "".join(f'VAR ups {k} "{v}"\n' for k, v in variables.items())
                    writer.write(f"BEGIN LIST VAR ups\n{body}END LIST VAR ups\n".encode())
            elif line == "LOGOUT":
                writer.write(b"OK Goodbye\n")
                break
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def run(snap):
    s = {"enabled": True}
    th = {d.id: Thresholds(d.warn, d.error, d.default_on) for d in plugin.checks}
    return {r.key.split(".")[1]: r for r in plugin.evaluate(s, th, (s, snap, "", NOW))}


async def test_on_mains_then_on_battery():
    good = {"ups.status": "OL CHRG", "battery.charge": "100", "battery.runtime": "2220", "ups.load": "13",
            "input.voltage": "237", "ups.mfr": "APC", "ups.model": "BX1600MI"}
    server, port = await fake_nut(good)
    async with server:
        snap = await plugin.fetch({"host": "127.0.0.1", "port": port, "ups": ""}, "", NOW)
    assert snap["name"] == "ups" and snap["_summary"] == "ups: APC BX1600MI"
    res = run(snap)
    assert all(r.status == OK for r in res.values()) and res["runtime"].value == 37
    res = run({"name": "ups", "vars": good | {"ups.status": "OB DISCHRG RB", "battery.charge": "40",
                                              "battery.runtime": "200"}})
    assert res["power"].status == ERROR and res["health"].status == ERROR
    assert res["battery"].status == WARNING and res["runtime"].status == ERROR


async def test_stale_data():
    server, port = await fake_nut({}, stale=True)
    async with server:
        with pytest.raises(ApiError, match="stale"):
            await plugin.fetch({"host": "127.0.0.1", "port": port, "ups": "ups"}, "", NOW)


async def test_unreachable():
    with pytest.raises(ApiError, match="can't reach"):
        await plugin.fetch({"host": "127.0.0.1", "port": 1, "ups": "ups"}, "", NOW)
