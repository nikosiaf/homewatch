"""Monitoring modules. v0.1 ships one: internet."""

from __future__ import annotations

from dataclasses import dataclass, field

OK, WARNING, ERROR = 0, 1, 2
LEVEL_WORD = {OK: "OK", WARNING: "WARNING", ERROR: "ERROR"}

# (id, name, available in this release); the rest show greyed out as "coming soon"
CATALOG = [
    ("internet", "Internet line", True),
    ("proxmox", "Proxmox VE", False),
    ("opnsense", "OPNsense", False),
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
