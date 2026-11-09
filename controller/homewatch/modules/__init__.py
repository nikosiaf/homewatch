"""Monitoring modules. v0.1 ships one: internet."""

from __future__ import annotations

from dataclasses import dataclass, field

OK, WARNING, ERROR = 0, 1, 2
LEVEL_WORD = {OK: "OK", WARNING: "WARNING", ERROR: "ERROR"}

# (id, name, available in this release); the rest show greyed out as "coming soon"
CATALOG = [
    ("internet", "Internet line", True),
    ("speedport", "ISP modem", True),
    ("proxmox", "Proxmox VE", True),
    ("opnsense", "OPNsense", True),
    ("homeassistant", "Home Assistant", True),
    ("pbs", "Proxmox Backup Server", True),
    ("synology", "Synology NAS", True),
    ("mikrotik", "MikroTik", True),
    ("unifi", "UniFi", True),
    ("ups", "UPS (NUT)", True),
    ("docker", "Docker hosts", True),
]


@dataclass(frozen=True)
class CheckDef:
    """A check a module offers. Thresholds are defaults; the admin can change them per check."""

    id: str
    name: str
    probe: str
    unit: str = ""
    warn: float | None = None
    error: float | None = None
    editable: tuple[str, ...] = ()  # which of "warn" / "error" the admin may edit
    lower_is_worse: bool = False  # e.g. line speed: below the threshold is bad
    help: str = ""
    default_on: bool = True


@dataclass
class Thresholds:
    warn: float | None
    error: float | None
    enabled: bool = True


@dataclass
class CheckResult:
    key: str
    name: str
    status: int
    detail: str
    value: float | None = None
    unit: str = ""
    suppressed_by: str | None = None  # the check whose failure explains this one
    extra: dict = field(default_factory=dict)
    unknown: bool = False  # OK only because nothing has been read yet
    observed_at: float | None = None  # when the reading behind this result was taken, if it can be reused between runs

    @property
    def effective(self) -> int:
        """The level the engine alerts on: suppressed checks follow their root cause instead."""
        return OK if self.suppressed_by else self.status


def level(value: float | None, t: Thresholds, lower_is_worse: bool = False) -> int:
    """Map a value to OK / WARNING / ERROR against thresholds (None = that level is off)."""
    if value is None:
        return OK
    if lower_is_worse:
        if t.error is not None and value < t.error:
            return ERROR
        if t.warn is not None and value < t.warn:
            return WARNING
        return OK
    if t.error is not None and value >= t.error:
        return ERROR
    if t.warn is not None and value >= t.warn:
        return WARNING
    return OK


@dataclass
class DevEvent:
    """Something a device did, with its time: a reboot, a line resync, a port down, the UPS on battery.
    exact: the device told the time itself; else it is the time of the reading that saw it."""

    ts: float
    kind: str  # reboot | line_resync | on_battery | on_mains | port_down | gateway_down | gateway_up
    device: str
    detail: str
    exact: bool = True


REBOOT_SLACK = 300  # s: a boot time that moved less than this is the same boot (rounded uptimes, clock drift)


def booted(prev: dict, key: str, boot: float | None, device: str, what: str, events: list) -> None:
    """Remember a device's boot time in prev[key]; a later one is a reboot (the first reading only remembers)."""
    if not boot:
        return
    old = prev.get(key)
    if old is not None and boot - old > REBOOT_SLACK:
        events.append(DevEvent(boot, "reboot", device, f"{what} restarted"))
    if old is None or abs(boot - old) > REBOOT_SLACK:
        prev[key] = boot


class Module:
    """What the runtime and the web pages need from a module."""

    id = ""
    name = ""
    checks: list[CheckDef] = []
    description = ""  # shown on the Help page, with setup
    setup: list[tuple[str, str]] = []  # ("text" | "code", content)
    setup_title = "First create a read-only account for homewatch"
    destination_keys = ("url", "host", "port", "devices")  # settings that decide where credentials go
    transport_keys = ("https", "verify")  # settings that protect them on the way, on or off
    icon = "server"  # the Status page tile's line icon (static/icons in _icons.html)
    headline: tuple[str, str] = ("", "")  # (check id, label): the number the Status tile shows big, with its 24 h line

    def series(self, site: str) -> list[str]:
        """The latest reading as detailed metrics (Influx line protocol, one line per device/port/guest...),
        written to VictoriaMetrics for the Grafana dashboards. Names: hw_<module>_<what>, field = the value.
        Called once per new reading (see reading_ts); [] when the module keeps nothing."""
        return []

    def load(self, rt) -> dict:
        raise NotImplementedError

    def transport(self, rt) -> tuple[str, bool] | None:
        """How the connection to the device is protected and whether that is good enough; None: it has no device."""
        return None

    def is_set_up(self, rt) -> bool:
        return self.configured(rt) if hasattr(self, "configured") else True

    def observed(self, results: list[CheckResult]) -> bool:
        """Did this run really see the device? The module's first check says whether it could be read."""
        root = next((r for r in results if r.key == f"{self.id}.{self.checks[0].id}"), None)
        if root is None:
            return any(not r.suppressed_by for r in results)
        return root.status == OK and not root.unknown and not root.suppressed_by

    def reading_ts(self, rt) -> float | None:
        """When the latest reading was taken, for modules that keep one between runs."""
        return (getattr(self, "snap_ts", 0) or None) if getattr(self, "snap", None) else None

    def refresh_soon(self, rt) -> None:
        """A kept reading showed a change: read again for the next run, so that a new reading confirms it."""
        if hasattr(self, "snap_ts"):
            self.snap_ts = 0

    def device_events(self, rt, prev: dict) -> list[DevEvent]:
        """What the devices did since the previous reading (reboots, a line resync, the UPS on battery...).
        Called once per new reading; prev is this module's memory between readings, kept by the runtime
        (change it in place). [] when the module has nothing to tell."""
        return []

    def secret_keys(self) -> list[str]:
        """Names of the module's entries in the encrypted secrets."""
        return []

    def forget_credentials(self, rt) -> None:
        for k in self.secret_keys():
            rt.secrets.delete(k)

    def imported(self, settings: dict) -> tuple[dict, str]:
        """Settings from an imported file, held to the rules of the module's form: (what to store, what is wrong)."""
        return settings, ""

    def enabled(self, settings: dict) -> bool:
        return bool(settings.get("enabled", True))

    async def readings(self, rt, settings: dict, now: float):
        raise NotImplementedError

    def evaluate(self, settings: dict, thresholds: dict, readings) -> list[CheckResult]:
        raise NotImplementedError

    def save(self, rt, form: dict, user) -> str:
        """Validate and store the settings form; return an error message or ''."""
        raise NotImplementedError

    def page_ctx(self, rt) -> dict:
        return {}

    def loops(self, rt) -> list:
        """Coroutines to run in the background (collectors)."""
        return []
