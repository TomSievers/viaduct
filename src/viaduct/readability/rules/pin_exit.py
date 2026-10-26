"""pin_exit: wires must leave a pin straight outward, one wire per pin."""

from __future__ import annotations

import math

from viaduct.readability.rules.base import Rule, key, same, unit, wire_ends


class PinExit(Rule):
    """A wire should leave a pin in the pin's own direction, away from the body, and run at
    least min_stub (mm) before it turns. Only one wire should leave a pin: a net that fans out
    branches at a junction away from the pin, so it is clear the pin is the source. Wires
    leaving sideways or doubling back along the body read as a connection to the next pin.
    Power symbols and power flags are net markers, like labels, and are left to
    power_orientation; parts the config counts as passive ([readability.passives]) are left
    to passive_pin_exit."""
    id = "pin_exit"
    title = "Wire leaves a pin badly"
    severity = "warning"
    options = {"min_stub": 1.27, "sheet_pins": True}
    passives = False                    # which side of the passive split this rule checks

    def check(self, sheet, options):
        wires = [w for w in sheet.wires if not w.bus]
        ends = wire_ends(wires)
        passive = {s.ref for s in sheet.symbols if s.passive}
        for pin in sheet.pins:
            if pin.power or (pin.on_sheet and not options.get("sheet_pins", False)):
                continue
            if (not pin.on_sheet and pin.owner in passive) != self.passives:
                continue
            leaving = ends.get(key(pin.tip), [])
            problems, fixes = [], []
            if len(leaving) > 1:
                problems.append(f"{len(leaving)} wires leave it")
                fixes.append("run one wire out from the pin and branch at a junction further out")
            sideways = [w for w, far in leaving if _dot(unit(pin.tip, far), pin.outward) < 0.999]
            if sideways:
                problems.append("a wire leaves it sideways or backwards" if len(sideways) == 1
                                else f"{len(sideways)} wires leave it sideways or backwards")
                fixes.append("leave the pin straight outward, then turn")
            for w, far in leaving:
                d = unit(pin.tip, far)
                stub = math.dist(pin.tip, far)
                if w not in sideways and stub < options["min_stub"] - 1e-6 and _turns(ends, w, far, d):
                    problems.append(f"the wire turns {stub:.2f} mm out")
                    fixes.append(f"run at least {options['min_stub']:g} mm straight out first")
            if problems:
                yield self.finding(f"{pin.label}: " + "; ".join(problems), pin.tip,
                                   [pin.owner, *(w.uuid for w, _ in leaving)], "; ".join(fixes))


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _turns(ends, w, at, d) -> bool:
    """Whether another wire continues from at in a different direction than d."""
    for v, far in ends.get(key(at), []):
        if v is not w and not same(unit(at, far), d):
            return True
    return False
