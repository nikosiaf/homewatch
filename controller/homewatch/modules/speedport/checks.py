"""Speedport checks: pure functions from the last modem reading to results.

Diagnosis order: modem not readable -> everything else suppressed; DSL link down -> sync,
SNR and resyncs suppressed (the outage itself is the message).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import ERROR, OK, CheckDef, CheckResult, Thresholds, level

MODULE = "speedport"

CHECKS: list[CheckDef] = [
    CheckDef("readable", "Modem readable", "modem",
             help="ERROR when homeWatch could not log in to or read the modem for 2.5 read intervals, "
                  "or at once when the modem's certificate changed."),
    CheckDef("link", "DSL link", "modem", help="ERROR when the modem reports the DSL line down."),
    CheckDef("sync", "Sync rate", "modem", unit="%", warn=85, error=50, editable=("warn", "error"),
             lower_is_worse=True,
             help="Lower of download and upload sync as a share of the contracted speed (Internet line page). "
                  "Without a contracted speed it only shows the rates."),
    CheckDef("snr", "SNR margin", "modem", unit="dB", warn=6, error=3, editable=("warn", "error"),
             lower_is_worse=True, help="Lower of the downstream and upstream noise margin."),
    CheckDef("resyncs", "Resyncs (24 h)", "modem", warn=3, error=5, editable=("warn", "error"),
             help="How often the DSL line re-trained in the last 24 hours. Each resync is also an event and a Grafana "
                  "marker; one now and then is normal, so the WARNING starts at 3."),
    CheckDef("ppp", "PPP session on the modem", "modem", default_on=False,
             help="Only when the modem itself dials PPPoE (router mode). Off by default for bridge setups."),
]
CHECK_BY_ID = {c.id: c for c in CHECKS}


@dataclass
class State:
    now: float
    interval: float
    last_ok: float | None = None
    last_error: str = ""
    rejected: bool = False
    cert_changed: bool = False
    values: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    resyncs_24h: float | None = None
    contract_down: float = 0
    contract_up: float = 0


def _ago(s: float) -> str:
    m = int(s // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"


def evaluate(settings: dict, th: dict[str, Thresholds], st: State) -> list[CheckResult]:
    out: dict[str, CheckResult] = {}

    def add(cid, status, detail, value=None, suppressed_by=None):
        d = CHECK_BY_ID[cid]
        out[cid] = CheckResult(f"{MODULE}.{cid}", d.name, status, detail, value, d.unit,
                               f"{MODULE}.{suppressed_by}" if suppressed_by else None)

    root = None
    limit = 2.5 * st.interval
    if st.cert_changed:  # possibly an impostor: say so now, not after 2.5 read intervals
        add("readable", ERROR, f"Not logging in: {st.last_error}")
        root = "readable"
    elif st.last_ok is None:
        if st.last_error:
            add("readable", ERROR, f"Can't read the modem: {st.last_error}")
            root = "readable"
        else:
            add("readable", OK, "No reading yet")
            out["readable"].unknown = True
            return [out[c.id] for c in CHECKS if c.id in out and th[c.id].enabled]
    elif st.now - st.last_ok > limit:
        add("readable", ERROR, f"No modem reading for {_ago(st.now - st.last_ok)}"
                               + (f": {st.last_error}" if st.last_error else ""), st.now - st.last_ok)
        root = "readable"
    else:
        add("readable", OK, f"Read {_ago(st.now - st.last_ok)} ago", st.now - st.last_ok)

    if st.rejected and root is None:
        add("readable", ERROR, "The modem rejected the login; check the user name and password")
        root = "readable"

    rest = ("link", "sync", "snr", "resyncs", "ppp")
    if root:
        for c in rest:
            add(c, OK, "no reading", suppressed_by=root)
        return [out[c.id] for c in CHECKS if c.id in out and th[c.id].enabled]

    v, s = st.values, st.status
    if s.get("link_up") == 0:
        add("link", ERROR, "DSL line down: the modem has no sync")
        for c in ("sync", "snr", "resyncs"):
            add(c, OK, "line down", suppressed_by="link")
    else:
        up_for = v.get("line_up_s")
        add("link", OK, f"In sync for {_ago(up_for)}" if up_for else "In sync")
        down, up = v.get("sync_down_kbps"), v.get("sync_up_kbps")
        rates = f"{(down or 0) / 1000:.1f} / {(up or 0) / 1000:.1f} Mbit/s"
        if down is None or up is None:
            add("sync", OK, "No sync rate reported")
        elif st.contract_down and st.contract_up:
            share = min(down / 1000 / st.contract_down, up / 1000 / st.contract_up) * 100
            add("sync", level(share, th["sync"], True),
                f"{rates} ({share:.0f} % of {st.contract_down:g} / {st.contract_up:g})", share)
        else:
            add("sync", OK, rates)
        snrs = [x for x in (v.get("snr_down_db"), v.get("snr_up_db")) if x is not None]
        if snrs:
            m = min(snrs)
            add("snr", level(m, th["snr"], True),
                f"{v.get('snr_down_db', 0):.1f} dB down / {v.get('snr_up_db', 0):.1f} dB up", m)
        else:
            add("snr", OK, "No SNR reported")
        if st.resyncs_24h is not None:
            n = st.resyncs_24h
            add("resyncs", level(n, th["resyncs"]), f"{n:.0f} resync{'s' if n != 1 else ''} in 24 h", n)
        else:
            add("resyncs", OK, "Not enough history yet")

    if "ppp_up" in s:
        add("ppp", OK if s["ppp_up"] else ERROR, "PPP session up" if s["ppp_up"] else "PPP session down")

    return [out[c.id] for c in CHECKS if c.id in out and th[c.id].enabled]
