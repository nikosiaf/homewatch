"""All modules available in this release, by id."""

from .homeassistant.plugin import plugin as homeassistant
from .internet.plugin import plugin as internet
from .mikrotik.plugin import plugin as mikrotik
from .opnsense.plugin import plugin as opnsense
from .pbs.plugin import plugin as pbs
from .proxmox.plugin import plugin as proxmox
from .speedport.plugin import plugin as speedport
from .synology.plugin import plugin as synology
from .unifi.plugin import plugin as unifi

MODULES = {m.id: m for m in (internet, speedport, proxmox, opnsense, homeassistant, synology, mikrotik, unifi, pbs)}
