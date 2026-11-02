"""long_wire: straight wire runs longer than a set length."""

from __future__ import annotations

from collections import defaultdict

from viaduct.readability.rules.base import Rule
from viaduct.readability.sheet import TOL


class LongWire(Rule):
    """A straight run of wire, joined segments included, is longer than max_length (mm).
    Long runs across the sheet are hard to follow by eye; a pair of net labels says the same
    with nothing to trace. Long runs are a common, readable style on large sheets, so this
    rule is off by default; turn it on with a severity in the config."""
    id = "long_wire"
    title = "Long wire"
    severity = "ignore"
    options = {"max_length": 76.2}

    def check(self, sheet, options):
        runs = defaultdict(list)            # (axis, offset) -> [(start, end, uuid)]
        for w in sheet.wires:
            if w.bus:
                continue
            if w.horizontal:
                runs[("h", round(w.a[1], 3))].append((min(w.a[0], w.b[0]), max(w.a[0], w.b[0]), w.uuid))
            elif w.vertical:
                runs[("v", round(w.a[0], 3))].append((min(w.a[1], w.b[1]), max(w.a[1], w.b[1]), w.uuid))
        for (axis, off), spans in runs.items():
            spans.sort()
            merged = []
            for s, e, u in spans:
                if merged and s <= merged[-1][1] + TOL:
                    merged[-1][1] = max(merged[-1][1], e)
                    merged[-1][2].append(u)
                else:
                    merged.append([s, e, [u]])
            for s, e, uuids in merged:
                if e - s > options["max_length"]:
                    at = ((s + e) / 2, off) if axis == "h" else (off, (s + e) / 2)
                    yield self.finding(f"straight wire runs {e - s:.1f} mm", at, uuids,
                                       "replace the long run with net labels at both ends")
