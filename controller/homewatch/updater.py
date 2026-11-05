"""The host's updater, as homewatch sees it.

install.sh can add a small systemd unit on the host that installs the latest release when homewatch asks.
homewatch has no Docker access: it only leaves a file in <update dir>/ask/, and the host's script (deploy/update.sh)
reads the latest version from GitHub itself, so the request can't choose a version. The script writes how it went
to <update dir>/status as "<unix time> <running|ok|failed> <text>".
"""

from __future__ import annotations

import os
import time

STALE = 15 * 60  # "running" for longer than this: the updater is gone or stuck


def state(update_dir: str, now: float | None = None) -> dict:
    """{ready, state, status}: ready = the updater is installed and homewatch may ask it."""
    ask = os.path.join(update_dir, "ask")
    ready = os.path.isdir(ask) and os.access(ask, os.W_OK)
    out = {"ready": ready, "state": "", "status": "", "asked": os.path.exists(os.path.join(ask, "request"))}
    try:
        with open(os.path.join(update_dir, "status"), encoding="utf-8") as f:
            raw = f.read(500).strip()
    except OSError:
        raw = ""
    parts = raw.split(" ", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1] in ("running", "ok", "failed"):
        ts, st, text = int(parts[0]), parts[1], parts[2]
        now = now or time.time()
        if st == "running" and now - ts > STALE:
            st, text = "failed", f"{text}, no answer since then (see journalctl -u homewatch-update)"
        out |= {"state": st, "status": f"{text} ({time.strftime('%d %b %H:%M', time.localtime(ts))})"}
    if out["asked"] and out["state"] != "running":
        out |= {"state": "running", "status": "update requested, waiting for the updater"}
    return out


def ask(update_dir: str, version: str, username: str) -> str:
    """Ask the host to install the latest release. Returns an error message or ''."""
    if not state(update_dir)["ready"]:
        return "The updater isn't installed on this host: run install.sh again, or update by hand"
    try:
        with open(os.path.join(update_dir, "ask", "request"), "w", encoding="utf-8") as f:
            f.write(f"{int(time.time())} {username} {version}\n")
    except OSError as e:
        return f"Can't ask the updater ({e.strerror})"
    return ""
