"""All modules available in this release, by id."""

from .internet.plugin import plugin as internet
from .speedport.plugin import plugin as speedport

MODULES = {m.id: m for m in (internet, speedport)}
