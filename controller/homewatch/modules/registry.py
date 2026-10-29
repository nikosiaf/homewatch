"""All modules available in this release, by id."""

from .internet.plugin import plugin as internet
from .opnsense.plugin import plugin as opnsense
from .proxmox.plugin import plugin as proxmox
from .speedport.plugin import plugin as speedport

MODULES = {m.id: m for m in (internet, speedport, proxmox, opnsense)}
