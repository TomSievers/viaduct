"""The readability rules, one per module. RULES is the list check_readability runs on every
sheet, in this order; add a rule by writing its module and listing it here."""

from viaduct.readability.rules.base import Finding, Rule
from viaduct.readability.rules.diagonal_wire import DiagonalWire
from viaduct.readability.rules.four_way_junction import FourWayJunction
from viaduct.readability.rules.long_wire import LongWire
from viaduct.readability.rules.missing_junction import MissingJunction
from viaduct.readability.rules.off_grid import OffGrid
from viaduct.readability.rules.passive_pin_exit import PassivePinExit
from viaduct.readability.rules.pin_exit import PinExit
from viaduct.readability.rules.power_orientation import PowerOrientation
from viaduct.readability.rules.small_text import SmallText
from viaduct.readability.rules.symbol_overlap import SymbolOverlap
from viaduct.readability.rules.text_overlap import TextOverlap
from viaduct.readability.rules.wire_bends import WireBends
from viaduct.readability.rules.wire_crossing import WireCrossing
from viaduct.readability.rules.wire_over_pin import WireOverPin
from viaduct.readability.rules.wire_overlap import WireOverlap
from viaduct.readability.rules.wire_through_symbol import WireThroughSymbol
from viaduct.readability.rules.wire_through_text import WireThroughText

RULES: list[Rule] = [
    # wires against other items
    WireThroughText(),
    WireThroughSymbol(),
    WireOverPin(),
    WireOverlap(),
    WireCrossing(),
    # connections
    MissingJunction(),
    FourWayJunction(),
    PinExit(),
    PassivePinExit(),
    # text and parts
    TextOverlap(),
    SymbolOverlap(),
    SmallText(),
    # style
    DiagonalWire(),
    OffGrid(),
    PowerOrientation(),
    LongWire(),
    WireBends(),
]

BY_ID: dict[str, Rule] = {r.id: r for r in RULES}

__all__ = ["BY_ID", "RULES", "Finding", "Rule"]
