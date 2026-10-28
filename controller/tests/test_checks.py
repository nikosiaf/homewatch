"""Internet checks and the diagnosis order, from recorded-style readings."""

import copy

from homewatch.modules import ERROR, OK, WARNING, Thresholds
from homewatch.modules.internet import DEFAULTS
from homewatch.modules.internet.checks import CHECKS, Dns, Readings, Speed, Target, evaluate

NOW = 1_800_000_000.0


def settings(**over):
    s = copy.deepcopy(DEFAULTS)
    s["gateway"]["target"] = "192.168.1.1"
    s["dns"]["server"] = "192.168.1.1"
    s["speed"].update(contract_down=100, contract_up=10)
    for k, v in over.items():
        s[k].update(v)
    return s


def th(**over):
    t = {c.id: Thresholds(c.warn, c.error, True) for c in CHECKS}
    t.update(over)
    return t


def good(loss=0.0, rtt=12.0, jitter=1.0, ts=NOW - 20):
    return Target(loss, loss, rtt, jitter, ts)


def readings(**over):
    r = Readings(now=NOW, public={"1.1.1.1": good(), "8.8.8.8": good(rtt=15)}, gateway=good(rtt=0.5),
                 dns=Dns(True, "success", 8.0, NOW - 30),
                 speeds=[Speed(NOW - 100, 95, 9.5, True), Speed(NOW - 10900, 96, 9.6, True)])
    for k, v in over.items():
        setattr(r, k, v)
    return r


def by_id(results):
    return {r.key.split(".")[1]: r for r in results}


def test_all_ok():
    res = by_id(evaluate(settings(), th(), readings()))
    assert {k: r.status for k, r in res.items()} == {k: OK for k in
                                                     ("fresh", "gateway", "reachable", "loss", "latency", "jitter",
                                                      "dns", "speed")}
    assert "best target 1.1.1.1" in res["latency"].detail


def test_lossy_line_warns_and_errors():
    r = readings(public={"1.1.1.1": good(loss=12), "8.8.8.8": good(loss=15)})
    assert by_id(evaluate(settings(), th(), r))["loss"].status == WARNING
    r = readings(public={"1.1.1.1": good(loss=25), "8.8.8.8": good(loss=30)})
    assert by_id(evaluate(settings(), th(), r))["loss"].status == ERROR


def test_one_bad_target_does_not_alarm():
    r = readings(public={"1.1.1.1": good(), "8.8.8.8": good(loss=100)})
    res = by_id(evaluate(settings(), th(), r))
    assert res["reachable"].status == OK
    assert "8.8.8.8 not answering" in res["reachable"].detail
    assert res["loss"].status == OK


def test_line_down_suppresses_quality_and_dns():
    r = readings(public={"1.1.1.1": good(loss=100), "8.8.8.8": good(loss=100)},
                 dns=Dns(False, "timeout", None, NOW - 30))
    res = by_id(evaluate(settings(), th(), r))
    assert res["reachable"].status == ERROR and "Internet line down" in res["reachable"].detail
    for c in ("loss", "latency", "jitter", "dns"):
        assert res[c].suppressed_by == "internet.reachable"
        assert res[c].effective == OK


def test_gateway_down_is_the_root_cause():
    r = readings(gateway=good(loss=100), public={"1.1.1.1": good(loss=100), "8.8.8.8": good(loss=100)})
    res = by_id(evaluate(settings(), th(), r))
    assert res["gateway"].status == ERROR and "Router or LAN problem" in res["gateway"].detail
    assert res["reachable"].suppressed_by == "internet.gateway"
    assert res["dns"].suppressed_by == "internet.gateway"


def test_dns_failing_while_ping_works():
    res = by_id(evaluate(settings(), th(), readings(dns=Dns(False, "timeout", None, NOW - 30))))
    assert res["dns"].status == ERROR and "DNS failing" in res["dns"].detail


def test_slow_dns_warns():
    res = by_id(evaluate(settings(), th(), readings(dns=Dns(True, "success", 650, NOW - 30))))
    assert res["dns"].status == WARNING


def test_latency_and_jitter():
    r = readings(public={"1.1.1.1": good(rtt=150, jitter=20), "8.8.8.8": good(rtt=160, jitter=35)})
    res = by_id(evaluate(settings(), th(), r))
    assert res["latency"].status == WARNING
    assert res["jitter"].status == WARNING  # best target 20 ms: warn 15, error 30


def test_no_data_means_monitoring_broken():
    r = readings(public={}, gateway=None, dns=None)
    res = by_id(evaluate(settings(), th(), r))
    assert res["fresh"].status == ERROR
    assert res["reachable"].suppressed_by == "internet.fresh"


def test_stale_data():
    old = NOW - 600
    r = readings(public={"1.1.1.1": good(ts=old)}, gateway=good(ts=old), dns=Dns(True, "success", 8, old))
    assert by_id(evaluate(settings(), th(), r))["fresh"].status == ERROR


def test_speed_needs_two_low_tests():
    one_low = [Speed(NOW - 100, 40, 9, True), Speed(NOW - 10900, 95, 9.5, True)]
    assert by_id(evaluate(settings(), th(), readings(speeds=one_low)))["speed"].status == OK
    two_low = [Speed(NOW - 100, 40, 9, True), Speed(NOW - 10900, 60, 9.5, True)]
    res = by_id(evaluate(settings(), th(), readings(speeds=two_low)))
    assert res["speed"].status == WARNING and "2 tests in a row" in res["speed"].detail
    low_upload = [Speed(NOW - 100, 95, 5, True), Speed(NOW - 10900, 95, 6, True)]
    assert by_id(evaluate(settings(), th(), readings(speeds=low_upload)))["speed"].status == WARNING


def test_failed_speed_tests_are_ignored():
    speeds = [Speed(NOW - 100, None, None, False), Speed(NOW - 200, 40, 9, True), Speed(NOW - 300, 50, 9, True)]
    assert by_id(evaluate(settings(), th(), readings(speeds=speeds)))["speed"].status == WARNING


def test_speed_without_contract_is_ok():
    s = settings(speed={"contract_down": 0, "contract_up": 0})
    two_low = [Speed(NOW - 100, 4, 1, True), Speed(NOW - 10900, 6, 1, True)]
    res = by_id(evaluate(s, th(), readings(speeds=two_low)))
    assert res["speed"].status == OK and "no contracted speed" in res["speed"].detail


def test_probes_switched_off_hide_their_checks():
    s = settings(gateway={"enabled": False}, dns={"enabled": False}, speed={"enabled": False})
    res = by_id(evaluate(s, th(), readings()))
    assert set(res) == {"fresh", "reachable", "loss", "latency", "jitter"}


def test_disabled_check_and_empty_threshold():
    t = th(latency=Thresholds(None, None, True), jitter=Thresholds(15, 30, False))
    r = readings(public={"1.1.1.1": good(rtt=900, jitter=50)})
    res = by_id(evaluate(settings(), t, r))
    assert res["latency"].status == OK
    assert "jitter" not in res
