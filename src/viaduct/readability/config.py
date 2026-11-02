"""The readability config: each rule's severity and options, sheets to skip, and findings
that are accepted. It is the [readability] table of a TOML file, by default viaduct.toml
next to the root schematic:

    [readability]
    ignore_sheets = ["legacy/*.kicad_sch"]       # globs, relative to the root schematic

    [readability.passives]                       # which parts count as passive (passive_pin_exit)
    references = ["R", "C", "L", "FB"]           # reference prefixes: letters before the number
    lib_ids = ["MyLib:Shunt*"]                   # library symbol globs
    max_pins = 2                                 # visible pins at most

    [readability.rules.wire_crossing]
    severity = "ignore"                          # error, warning or ignore

    [readability.rules.long_wire]
    severity = "error"
    max_length = 120.0                           # a rule option, same type as its default

    [[readability.suppress]]                     # one accepted finding (or a group of them)
    rule = "text_overlap"                        # a rule id, or a glob
    sheet = "power.kicad_sch"                    # optional glob
    item = "R12"                                 # optional glob on a finding's items
    reason = "value sits inside the divider on purpose"

Everything is checked before a run: an unknown rule, option or key, or a value of the wrong
type, refuses the whole file with every problem listed, so a typo never silently turns a
rule off.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from viaduct.readability.rules import BY_ID, Finding, Rule
from viaduct.readability.sheet import Sheet, Symbol

FILE_NAME = "viaduct.toml"
SEVERITIES = ("error", "warning", "ignore")
_TOP_KEYS = {"rules", "ignore_sheets", "suppress", "passives"}
_SUPPRESS_KEYS = {"rule", "sheet", "item", "reason"}
# resistors, capacitors, inductors, ferrite beads, fuses, thermistors, varistors, crystals
PASSIVE_REFERENCES = ["R", "C", "L", "FB", "F", "RT", "RV", "Y"]


class ConfigError(ValueError):
    pass


@dataclass
class Suppress:
    rule: str
    sheet: str | None = None
    item: str | None = None
    reason: str | None = None

    def matches(self, f: Finding) -> bool:
        return (fnmatchcase(f.rule, self.rule)
                and (self.sheet is None or fnmatchcase(f.sheet, self.sheet))
                and (self.item is None or any(fnmatchcase(i, self.item) for i in f.items)))


@dataclass
class Passives:
    """Which parts count as passive: a reference prefix in references or a library symbol
    matching a glob in lib_ids, with at most max_pins visible pins. The pin limit keeps a
    3-pin potentiometer (RV) or a 4-pin crystal (Y) with the active parts."""
    references: list[str] = field(default_factory=lambda: list(PASSIVE_REFERENCES))
    lib_ids: list[str] = field(default_factory=list)
    max_pins: int = 2

    def matches(self, s: Symbol) -> bool:
        if len([p for p in s.pins if not p.hidden]) > self.max_pins:
            return False
        prefix = re.match(r"[A-Za-z]*", s.ref).group()
        return prefix in self.references or any(fnmatchcase(s.lib_id, g) for g in self.lib_ids)

    def mark(self, sheet: Sheet) -> None:
        for s in sheet.symbols:
            s.passive = self.matches(s)


@dataclass
class Config:
    path: Path | None = None
    severities: dict[str, str] = field(default_factory=dict)
    options: dict[str, dict] = field(default_factory=dict)
    ignore_sheets: list[str] = field(default_factory=list)
    suppress: list[Suppress] = field(default_factory=list)
    passives: Passives = field(default_factory=Passives)

    def severity(self, rule: Rule) -> str:
        return self.severities.get(rule.id, rule.severity)

    def options_for(self, rule: Rule) -> dict:
        return {**rule.options, **self.options.get(rule.id, {})}

    def skips(self, sheet: str) -> bool:
        return any(fnmatchcase(sheet, g) for g in self.ignore_sheets)

    def suppressed_by(self, f: Finding) -> Suppress | None:
        return next((s for s in self.suppress if s.matches(f)), None)

    def as_dict(self) -> dict:
        return {"file": str(self.path) if self.path else None, "severities": self.severities,
                "options": self.options, "ignore_sheets": self.ignore_sheets,
                "suppress": [s.__dict__ for s in self.suppress], "passives": self.passives.__dict__}


def find(root: Path) -> Path | None:
    """viaduct.toml next to the root schematic, if there is one."""
    path = root.parent / FILE_NAME
    return path if path.is_file() else None


def load(path: str | Path | None, rules: dict[str, Rule] = BY_ID) -> Config:
    """The config in path; with no path, every rule at its defaults."""
    if path is None:
        return Config()
    path = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: {e}") from None
    return parse(data.get("readability", {}), rules, path)


def parse(data: dict, rules: dict[str, Rule] = BY_ID, path: Path | None = None) -> Config:
    """A Config from the [readability] table, or ConfigError listing every problem in it."""
    problems: list[str] = []
    cfg = Config(path)
    if not isinstance(data, dict):
        raise ConfigError("[readability] must be a table")
    for k in data.keys() - _TOP_KEYS:
        problems.append(f"unknown key readability.{k} (expected one of {sorted(_TOP_KEYS)})")

    rule_tables = data.get("rules", {})
    if not isinstance(rule_tables, dict):
        problems.append("readability.rules must be a table of rule tables")
        rule_tables = {}
    for rid, table in rule_tables.items():
        where = f"readability.rules.{rid}"
        rule = rules.get(rid)
        if rule is None:
            problems.append(f"unknown rule {rid!r} (known: {', '.join(sorted(rules))})")
            continue
        if not isinstance(table, dict):
            problems.append(f"{where} must be a table")
            continue
        opts = {}
        for k, v in table.items():
            if k == "severity":
                if v not in SEVERITIES:
                    problems.append(f"{where}.severity must be one of {', '.join(SEVERITIES)}, not {v!r}")
                else:
                    cfg.severities[rid] = v
            elif k not in rule.options:
                known = ", ".join(["severity", *rule.options])
                problems.append(f"{where}: unknown option {k!r} (known: {known})")
            else:
                v, problem = _coerce(v, rule.options[k])
                if problem:
                    problems.append(f"{where}.{k}: {problem}")
                else:
                    opts[k] = v
        if opts:
            cfg.options[rid] = opts

    sheets = data.get("ignore_sheets", [])
    if not isinstance(sheets, list) or not all(isinstance(s, str) for s in sheets):
        problems.append("readability.ignore_sheets must be a list of globs")
    else:
        cfg.ignore_sheets = sheets

    entries = data.get("suppress", [])
    if not isinstance(entries, list):
        problems.append("readability.suppress must be an array of tables ([[readability.suppress]])")
        entries = []
    for n, s in enumerate(entries, 1):
        where = f"readability.suppress #{n}"
        if not isinstance(s, dict):
            problems.append(f"{where} must be a table")
            continue
        for k in s.keys() - _SUPPRESS_KEYS:
            problems.append(f"{where}: unknown key {k!r} (expected {', '.join(sorted(_SUPPRESS_KEYS))})")
        if not isinstance(s.get("rule"), str):
            problems.append(f"{where}: rule is required (a rule id or glob, e.g. \"*\")")
            continue
        if not any(fnmatchcase(r, s["rule"]) for r in rules):
            problems.append(f"{where}: rule {s['rule']!r} matches no rule")
        bad = [k for k in ("sheet", "item", "reason") if k in s and not isinstance(s[k], str)]
        problems += [f"{where}: {k} must be a string" for k in bad]
        if not bad:
            cfg.suppress.append(Suppress(s["rule"], s.get("sheet"), s.get("item"), s.get("reason")))

    passives = data.get("passives", {})
    if not isinstance(passives, dict):
        problems.append("readability.passives must be a table")
        passives = {}
    defaults = Passives()
    for k, v in passives.items():
        if not hasattr(defaults, k):
            problems.append(f"readability.passives: unknown key {k!r} (expected lib_ids, max_pins, references)")
            continue
        v, problem = _coerce(v, getattr(defaults, k))
        if problem is None and isinstance(v, list) and not all(isinstance(x, str) for x in v):
            problem = "expected a list of strings"
        if problem is None and k == "max_pins" and v < 1:
            problem = f"must be at least 1, not {v}"
        if problem is None and k == "references" and not all(re.fullmatch(r"[A-Za-z]+", r) for r in v):
            problem = "each reference prefix is letters only, e.g. \"FB\""
        if problem:
            problems.append(f"readability.passives.{k}: {problem}")
        else:
            setattr(cfg.passives, k, v)

    if problems:
        name = path.name if path else "config"
        raise ConfigError(f"{name} has {len(problems)} problem(s): " + "; ".join(problems))
    return cfg


def _coerce(value, default):
    """value checked against the type of default: (value, None), or (None, problem)."""
    want = type(default)
    if isinstance(value, bool) != (want is bool):       # TOML true is not 1, and 1 is not true
        return None, f"expected {want.__name__}, got {value!r}"
    if want is float and isinstance(value, int):
        return float(value), None
    if not isinstance(value, want):
        return None, f"expected {want.__name__}, got {value!r}"
    if want is list and default and not all(isinstance(v, type(default[0])) for v in value):
        return None, f"expected a list of {type(default[0]).__name__}"
    return value, None
