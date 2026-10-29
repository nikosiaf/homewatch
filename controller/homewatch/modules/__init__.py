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
    ("homeassistant", "Home Assistant", False),
    ("pbs", "Proxmox Backup Server", False),
    ("synology", "Synology NAS", False),
    ("mikrotik", "MikroTik", False),
    ("unifi", "UniFi", False),
    ("ups", "UPS (NUT)", False),
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

    def load(self, rt) -> dict:
        raise NotImplementedError

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
