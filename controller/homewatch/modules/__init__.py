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
