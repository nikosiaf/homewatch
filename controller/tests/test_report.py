"""The report for any period: the period itself, outages by likely cause, speed against the contract, the modem's
line, the CSV files, the page and /api/v1/report."""

import asyncio
import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from homewatch import causes, report
from homewatch.metrics import MetricsError
from homewatch.modules.internet import load as load_internet
from homewatch.modules.internet import save as save_internet
from tests.test_api import make_token
from tests.test_web import client, csrf  # noqa: F401 - the fixture

UTC = ZoneInfo("UTC")
D0 = datetime(2026, 10, 1, tzinfo=UTC).timestamp()  # the period: October 2026
D1 = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
NOW = datetime(2026, 11, 19, 12, tzinfo=UTC).timestamp()
H = 3600


class FakeMetrics:
    """Answers each query with the value of the first key it contains; MetricsError when down."""

    def __init__(self, values: dict | None = None, down: bool = False):
        self.values, self.down, self.asked = values or {}, down, []

    async def query(self, q, at=None):
        self.asked.append(q)
        if self.down:
            raise MetricsError("down")
        for k, v in self.values.items():
            if k in q:
                return [({}, at, v)]
        return []


def ev(db, ts, module, kind, device="modem", detail="", exact=1):
    db.run("INSERT INTO events(site,ts,module,device,kind,detail,exact,seen) VALUES('home',?,?,?,?,?,?,?)",
           (ts, module, device, kind, detail or kind, exact, ts))


def outage(db, start, minutes, cause=""):
    inc = db.run("INSERT INTO incidents(site,started,ended,worst,cause) VALUES('home',?,?,2,?)",
                 (start, start + minutes * 60, cause))
    db.run("INSERT INTO issues(site,check_key,level,worst,started,resolved,detail,notified,incident_id) "
           "VALUES('home','internet.reachable',2,2,?,?,'no answer',1,?)", (start, start + minutes * 60, inc))
    return inc


def watched(db, start=D0 - 86400, end=D1 + 86400, module="internet"):
    db.run("INSERT INTO coverage(site,module,start,end) VALUES('home',?,?,?)", (module, start, end))


# ---- the period -----------------------------------------------------------------------------------------------
def test_period_takes_local_dates_both_included():
    athens = ZoneInfo("Europe/Athens")
    start, end = report.period(athens, "2026-10-01", "2026-10-31", NOW)
    assert start == datetime(2026, 10, 1, tzinfo=athens).timestamp()
    assert end == datetime(2026, 11, 1, tzinfo=athens).timestamp()  # the 31st is included
    assert report.period(UTC, "2026-11-10", "2026-12-31", NOW) == (datetime(2026, 11, 10, tzinfo=UTC).timestamp(), NOW)
    assert report.period(UTC, str(D0), str(D0 + H), NOW) == (D0, D0 + H)  # Unix seconds, for scripts


@pytest.mark.parametrize("frm,to,msg", [("2026-10-10", "2026-10-01", "To is before From"),
                                        ("2026-12-01", "2026-12-02", "future"),
                                        ("2025-01-01", "2026-10-01", "At most 400 days"),
                                        ("1 Oct", "2026-10-02", "From: a date")])
def test_period_refuses_what_is_not_one(frm, to, msg):
    with pytest.raises(ValueError, match=msg):
        report.period(UTC, frm, to, NOW)


def test_quick_picks_previous_month_over_new_year():
    jan = datetime(2027, 1, 5, 9, tzinfo=UTC).timestamp()
    picks = {label.split(" (")[0]: (f, t) for label, f, t in report.quick_picks(UTC, jan)}
    assert picks["Previous month"] == ("2026-12-01", "2026-12-31")
    assert picks["Last 7 days"] == ("2026-12-30", "2027-01-05")
    assert picks["Last 30 days"] == ("2026-12-07", "2027-01-05")
    assert picks["This month"] == ("2027-01-01", "2027-01-05")


# ---- outages and their likely cause ---------------------------------------------------------------------------
def test_outages_split_by_likely_cause(db):
    watched(db)
    t_isp, t_home, t_unknown = D0 + 2 * 86400, D0 + 5 * 86400, D0 + 9 * 86400
    outage(db, t_isp, 10)
    ev(db, t_isp + 300, "speedport", "line_resync", detail="DSL line re-synced")
    outage(db, t_home, 30)
    ev(db, t_home, "ups", "on_battery", device="apc", exact=0)
    ev(db, t_home + 60, "speedport", "reboot", detail="The modem restarted")
    outage(db, t_unknown, 5)
    outage(db, D0 - 86400, 5)  # before the period: left out
    r = asyncio.run(report.gather(db, FakeMetrics(down=True), "home", D0, D1))
    assert [o.side for o in r.outages] == [causes.ISP, causes.HOME, causes.UNKNOWN]
    assert r.outages[0].cause.startswith("The DSL line dropped and re-synced")
    assert r.outages[1].cause.startswith("Power cut at home")
    sides = r.by_side()
    assert sides["isp"] == {"label": "ISP or line", "outages": 1, "down": 600}
    assert sides["home"]["outages"] == 1 and sides["home"]["down"] == 1800
    assert sides["unknown"]["outages"] == 1 and sides["unknown"]["down"] == 300
    assert r.availability.outages == 3 and r.availability.down == 2700  # the same outages availability counts
    assert r.availability.percent == pytest.approx(100 * (1 - 2700 / (D1 - D0)))


def test_outage_at_the_edge_counts_only_its_part_in_the_period(db):
    watched(db)
    outage(db, D1 - 600, 30)  # 10 of its 30 minutes are in October
    r = asyncio.run(report.gather(db, FakeMetrics(), "home", D0, D1))
    assert len(r.outages) == 1 and r.outages[0].down == 600 and r.outages[0].resolved is None  # still down at the end


def test_unwatched_outage_time_is_not_counted(db):
    watched(db, D0, D0 + 86400)
    outage(db, D0 + 3 * 86400, 60)  # homeWatch wasn't watching: unknown, not down
    r = asyncio.run(report.gather(db, FakeMetrics(), "home", D0, D1))
    assert r.outages == [] and r.availability.outages == 0 and r.availability.coverage == pytest.approx(100 / 31)


def test_cause_told_at_the_time_is_kept_when_the_events_are_gone(db):
    watched(db)
    outage(db, D0 + 86400, 10, cause="The DSL line dropped and re-synced at 08:05 with no power cut at home")
    r = asyncio.run(report.gather(db, FakeMetrics(), "home", D0, D1))
    assert r.outages[0].cause.startswith("The DSL line dropped") and r.outages[0].side == causes.UNKNOWN


def test_classify_names_the_side_of_each_rule(db):
    t = D0 + 86400
    ev(db, t, "ups", "on_battery", device="apc", exact=0)
    ev(db, t + 400, "speedport", "line_resync")
    cause, side, _ = causes.classify(db, "home", t, t + 420, ["internet.reachable"], UTC)
    assert cause.startswith("Power cut in the area") and side == causes.HOME
    db.run("INSERT INTO coverage(site,module,start,end) VALUES('home','speedport',?,?)", (D1, D1 + 86400))
    cause, side, _ = causes.classify(db, "home", D1 + H, D1 + 2 * H, ["internet.reachable"], UTC)
    assert "ISP's network" in cause and side == causes.ISP
    assert causes.explain(db, "home", D1 + H, D1 + 2 * H, ["internet.reachable"], UTC)[0] == cause


# ---- speed against the contract -------------------------------------------------------------------------------
def speedtests(db, downs, ups):
    for n, (d, u) in enumerate(zip(downs, ups)):
        db.run("INSERT INTO speedtests(site,ts,engine,down,up,latency,ok) VALUES('home',?,'cloudflare',?,?,12,1)",
               (D0 + n * 6 * H, d, u))
    db.run("INSERT INTO speedtests(site,ts,engine,down,up,latency,ok,error) VALUES('home',?,'cloudflare',NULL,NULL,"
           "NULL,0,'timeout')", (D0 + H,))  # a failed test isn't a slow one


def test_speed_against_the_contract_and_the_minimum(db):
    speedtests(db, [100, 80, 40, 20], [10, 9, 8, 2])
    s = load_internet(db)
    s["line"].update(contract_down=100, contract_up=10, min_down=50, min_up=5)
    save_internet(db, s)
    sp = asyncio.run(report.gather(db, FakeMetrics(), "home", D0, D1)).speed
    assert len(sp.tests) == 4
    assert sp.stat("down") == {"avg": 60, "min": 20, "max": 100}
    assert sp.share_of_contract("down") == 60 and sp.share_of_contract("up") == pytest.approx(72.5)
    assert sp.below_minimum("down") == (2, 50) and sp.below_minimum("up") == (1, 25)


def test_speed_without_a_contract_is_plain_numbers(db):
    speedtests(db, [100, 80], [10, 9])
    sp = asyncio.run(report.gather(db, FakeMetrics(), "home", D0, D1)).speed
    assert sp.stat("up") == {"avg": 9.5, "min": 9, "max": 10}
    assert sp.share_of_contract("down") is None and sp.below_minimum("down") is None


def test_no_speed_section_without_speed_tests(db):
    r = asyncio.run(report.gather(db, FakeMetrics(), "home", D0, D1))
    assert r.speed is None  # the speed test is off and none ran
    s = load_internet(db)
    s["speed"]["enabled"] = True
    save_internet(db, s)
    assert asyncio.run(report.gather(db, FakeMetrics(), "home", D0, D1)).speed.tests == []


# ---- the modem's line -----------------------------------------------------------------------------------------
def test_modem_line_only_when_the_module_is_on(db):
    ev(db, D0 + H, "speedport", "line_resync")
    ev(db, D0 + 2 * H, "speedport", "line_resync")
    ev(db, D0 + 3 * H, "speedport", "line_down")
    ev(db, D1 + H, "speedport", "line_resync")  # after the period
    m = FakeMetrics({"avg_over_time(hw_dsl_sync_down_kbps": 98000, "min_over_time(hw_dsl_sync_down_kbps": 61000,
                     "avg_over_time(hw_dsl_snr_down_db": 9.5, "min_over_time(hw_dsl_snr_down_db": 3.2})
    assert asyncio.run(report.gather(db, m, "home", D0, D1)).line is None
    assert not any("hw_dsl" in q for q in m.asked)
    db.set("module.speedport", {"enabled": True})
    line = asyncio.run(report.gather(db, m, "home", D0, D1)).line
    assert (line["resyncs"], line["drops"], line["restarts"]) == (2, 1, 0)
    v = {x["label"]: x for x in line["values"]}
    assert v["Sync rate down"]["avg"] == 98 and v["Sync rate down"]["worst"] == 61
    assert v["SNR margin down"]["worst"] == 3.2 and v["Sync rate up"]["avg"] is None
    assert f"[{int(D1 - D0)}s]" in m.asked[-1]  # over the whole period
    down = asyncio.run(report.gather(db, FakeMetrics(down=True), "home", D0, D1)).line
    assert down["values"] is None and down["resyncs"] == 2  # the counts come from what homeWatch stored


# ---- the weekly message is the same report over 7 days ---------------------------------------------------------
def test_weekly_message_uses_the_period_report(db):
    now = D0 + 7 * 86400
    watched(db)
    outage(db, D0 + 86400, 10)
    speedtests(db, [100, 80], [10, 9])
    s = load_internet(db)
    s["line"].update(contract_down=100, contract_up=10)
    save_internet(db, s)
    m = FakeMetrics({"ping_average_response_ms": 14.2})
    msg = asyncio.run(report.build(db, m, "home", now))
    assert "1 outage, 10 min down in total · monitored 100 % of the week" in msg.body
    assert "Speed: average 90.0 / 9.5 Mbit/s (contract 100 / 10), lowest download 80.0 Mbit/s, 2 tests" in msg.body
    assert "Latency: average 14.2 ms, worst 5-min 14.2 ms" in msg.body
    assert "[604800s]" in m.asked[0] and not any("hw_dsl" in q for q in m.asked)


# ---- CSV, page and API -----------------------------------------------------------------------------------------
def test_csv_files(db):
    watched(db)
    outage(db, D0 + 86400, 10)
    ev(db, D0 + 86400 + 300, "speedport", "line_resync")
    speedtests(db, [100.04], [10])
    r = asyncio.run(report.gather(db, FakeMetrics(), "home", D0, D1))
    rows = list(csv.reader(io.StringIO(report.csv_outages(r, UTC))))
    assert rows[0] == ["started", "ended", "down_minutes", "side", "likely_cause"]
    assert rows[1][:4] == ["2026-10-02T00:00:00+00:00", "2026-10-02T00:10:00+00:00", "10.0", "ISP or line"]
    assert rows[1][4].startswith("The DSL line dropped")
    rows = list(csv.reader(io.StringIO(report.csv_speedtests(r, UTC))))
    assert rows == [["time", "engine", "download_mbps", "upload_mbps", "latency_ms"],
                    ["2026-10-01T00:00:00+00:00", "cloudflare", "100.0", "10.0", "12.0"]]


def logged_in(client):  # noqa: F811
    token, tok = make_token(client)
    return token


def test_report_page_csv_and_print(client):  # noqa: F811
    logged_in(client)
    db = client.rt.db
    watched(db)
    outage(db, D0 + 86400, 10)
    ev(db, D0 + 86400 + 300, "speedport", "line_resync")
    assert 'href="/history/report"' in client.get("/history").text
    page = client.get("/history/report?from=2026-10-01&to=2026-10-31").text
    assert "01 Oct 2026 – 31 Oct 2026" in page and "Outages by likely cause" in page and "ISP or line" in page
    assert "data-print" in page and "/history/report.csv?what=outages&amp;from=2026-10-01&amp;to=2026-10-31" in page
    assert "Modem line" not in page and "Speed tests" not in page  # modules or probes that are off are left out
    assert "Last 7 days" in page and "Previous month" in page
    r = client.get("/history/report.csv?what=outages&from=2026-10-01&to=2026-10-31")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="homewatch-outages-2026-10-01-to-2026-10-31.csv"' in r.headers["content-disposition"]
    assert "ISP or line" in r.text
    assert client.get("/history/report.csv?what=x&from=2026-10-01&to=2026-10-31").status_code == 400
    assert "To is before From" in client.get("/history/report?from=2026-10-10&to=2026-10-01").text
    assert client.get("/history/report").status_code == 200  # the last 30 days by default


def test_report_page_leaves_out_modules_that_are_off(client):  # noqa: F811
    logged_in(client)
    s = load_internet(client.rt.db)
    s["enabled"] = False
    save_internet(client.rt.db, s)
    page = client.get("/history/report?from=2026-10-01&to=2026-10-31").text
    assert "Availability" not in page and "Outages (CSV)" not in page and "nothing to report" in page


def test_viewer_sees_the_report(client):  # noqa: F811
    from homewatch.auth import Auth

    logged_in(client)
    Auth(client.rt.db).create_user("viewer", "a-long-password", "viewer")
    client.post("/logout", data={"csrf": csrf(client.get("/history").text)})
    client.post("/login", data={"username": "viewer", "password": "a-long-password",
                                "csrf": csrf(client.get("/login").text)})
    assert client.get("/history/report").status_code == 200
    assert client.get("/history/report.csv?what=speedtests&from=2026-10-01&to=2026-10-31").status_code == 200


def test_api_report(client):  # noqa: F811
    token = logged_in(client)
    h = {"Authorization": f"Bearer {token}"}
    db = client.rt.db
    watched(db)
    outage(db, D0 + 86400, 10)
    ev(db, D0 + 86400 + 300, "speedport", "line_resync")
    speedtests(db, [40], [10])
    s = load_internet(db)
    s["line"].update(contract_down=100, contract_up=10, min_down=50)
    save_internet(db, s)
    j = client.get("/api/v1/report?from=2026-10-01&to=2026-10-31", headers=h).json()
    assert j["from"] == D0 and j["to"] == D1 and j["availability"]["outages"] == 1
    assert j["outages"][0]["side"] == "isp" and j["outages"][0]["down_s"] == 600
    assert j["outages_by_side"]["isp"] == {"outages": 1, "down_s": 600} and j["outages_by_side"]["home"]["outages"] == 0
    assert j["speed"]["down"]["below_minimum"] == 1 and j["speed"]["down"]["percent_of_contract"] == 40
    assert j["speed"]["up"]["below_minimum"] is None and j["speed"]["min_up"] is None
    assert "line" not in j  # the modem module is off
    r = client.get("/api/v1/report?from=2026-10-31&to=2026-10-01", headers=h)
    assert r.status_code == 400 and r.json()["error"] == "To is before From"
    assert client.get("/api/v1/report").status_code == 401
    assert client.get("/api/v1/report", headers=h).status_code == 200


def test_internet_settings_keep_the_guaranteed_minimum(client):  # noqa: F811
    _, tok = make_token(client)
    client.post("/modules/internet", data={"csrf": tok, "_switch": "1", "enabled": "on", "public.enabled": "on",
                                           "public.targets": "1.1.1.1", "speed.engine": "cloudflare",
                                           "speed.interval_h": "6", "line.contract_down": "100",
                                           "line.contract_up": "10", "line.min_down": "60", "line.min_up": "-3"})
    assert load_internet(client.rt.db)["line"] == {"contract_down": 100, "contract_up": 10, "min_down": 60,
                                                   "min_up": 0}
    assert 'name="line.min_down" value="60' in client.get("/modules/internet").text
