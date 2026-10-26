"""small_text: visible text below a minimum character height."""

from __future__ import annotations

from viaduct.readability.rules.base import Rule


class SmallText(Rule):
    """Visible text is smaller than min_size (mm character height). KiCad's default is 1.27 mm;
    much below 1 mm is unreadable on a printed A4 or A3 sheet."""
    id = "small_text"
    title = "Text too small"
    severity = "warning"
    options = {"min_size": 1.0}

    def check(self, sheet, options):
        for t in sheet.texts:
            if t.kind != "text_box" and t.size < options["min_size"] - 1e-6:
                yield self.finding(f"{t.label} is {t.size:g} mm tall", t.at, [t.owner or t.text],
                                   f"make it at least {options['min_size']:g} mm, or hide the field")
