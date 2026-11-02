"""passive_pin_exit: pin_exit for passive parts."""

from __future__ import annotations

from viaduct.readability.rules.pin_exit import PinExit


class PassivePinExit(PinExit):
    """pin_exit for passive parts: a wire should leave each pin straight outward, run at least
    min_stub (mm) before it turns, and be the only wire at the pin. What counts as passive is
    set in [readability.passives]: by default two-pin parts whose reference starts with R, C,
    L, FB, F, RT, RV or Y. Many schematics bridge a passive between two wires with a corner or
    a T at its pin, which reads fine, so this rule is off by default."""
    id = "passive_pin_exit"
    title = "Wire leaves a passive's pin badly"
    severity = "ignore"
    options = {"min_stub": 1.27}
    passives = True
