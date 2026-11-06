"""The host's updater, as homeWatch sees it.

install.sh can add a small systemd unit on the host that installs the latest release when homewatch asks.
homeWatch has no Docker access: it only leaves a file in <update dir>/ask/, and the host's script (deploy/update.sh)
reads the latest version from GitHub itself, so the request can't choose a version. The script writes how it went
to <update dir>/status as "<unix time> <running|ok|failed> <text>".
"""

from __future__ import annotations

import os
import re
import time

STALE = 15 * 60  # "running" for longer than this: the updater is gone or stuck


def state(update_dir: str, now: float | None = None) -> dict:
    """{ready, state, status}: ready = the updater is installed and homeWatch may ask it."""
    ask = os.path.join(update_dir, "ask")
    ready = os.path.isdir(ask) and os.access(ask, os.W_OK)
    out = {"ready": ready, "state": "", "status": "", "summary": "", "asked": os.path.exists(os.path.join(ask, "request"))}
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
        when = time.strftime('%d %b %H:%M', time.localtime(ts))
        out |= {"state": st, "status": f"{text} ({when})", "summary": summary(st, text, when)}
    if out["asked"] and out["state"] != "running":
        out |= {"state": "running", "status": "update requested, waiting for the updater",
                "summary": "requested, waiting for the updater"}
    return out


def summary(st: str, text: str, when: str) -> str:
    """The last update in a few words, from the updater's own text."""
    if m := re.fullmatch(r"(\S+) installed \(was (\S+)\)", text):
        return f"{m[2]} → {m[1]} on {when}"
    if m := re.fullmatch(r"(\S+) is already installed", text):
        return f"{m[1]} was already installed ({when})"
    if m := re.fullmatch(r"installing (\S+) \(was (\S+)\)", text):
        return f"installing {m[1]} (from {m[2]}), started {when}"
    if m := re.fullmatch(r"installing (\S+) failed, still on (\S+)(.*)", text, re.S):
        return f"update to {m[1]} failed on {when}, still on {m[2]}{m[3]}"
    return f"{text} ({when})"


RETENTIONS = {"1y": "1 year", "180d": "6 months", "90d": "3 months", "30d": "1 month"}
COMMANDS = {"grafana on", "grafana off", *(f"retention {k}" for k in RETENTIONS)}


def features(update_dir: str) -> set[str]:
    """What the host's updater can do besides updating ('grafana', 'retention'); empty for older updaters."""
    try:
        with open(os.path.join(update_dir, "features"), encoding="utf-8") as f:
            return set(f.read(200).split())
    except OSError:
        return set()


def ask(update_dir: str, version: str, username: str, command: str = "") -> str:
    """Ask the host to install the latest release, or to run one of COMMANDS. Returns an error message or ''."""
    if not state(update_dir)["ready"]:
        return "The updater isn't installed on this host: run install.sh again, or update by hand"
    if command and (command not in COMMANDS or command.split()[0] not in features(update_dir)):
        return "The updater on this host is older and can't do that: download update.sh of this release next to install.sh"
    try:
        with open(os.path.join(update_dir, "ask", "request"), "w", encoding="utf-8") as f:
            # the updater compares the first line with its fixed words; anything else means "update"
            f.write(f"{command}\n" if command else f"{int(time.time())} {username} {version}\n")
    except OSError as e:
        return f"Can't ask the updater ({e.strerror})"
    return ""
