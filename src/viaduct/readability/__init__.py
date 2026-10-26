"""Schematic readability: a rule per layout problem (crossing wires, wires through labels,
wires leaving pins sideways, ...), checked on every sheet of a hierarchy, with severities
and accepted findings set in viaduct.toml. Read-only: the .kicad_sch files are parsed
directly, since KiCad's API has no schematic access."""

from viaduct.readability.check import check, describe, find_position, plan_fixes, query_region, route_wire
from viaduct.readability.config import ConfigError

__all__ = ["ConfigError", "check", "describe", "find_position", "plan_fixes", "query_region", "route_wire"]
