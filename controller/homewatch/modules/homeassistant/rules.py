"""Entity rules: the admin's own checks on any Home Assistant entity, one rule per line.

    <entity id or pattern> <is | is not | below | above> <value> <warning | error>

A pattern may use * and ? (binary_sensor.panel_troubles_*). Lines starting with # are notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .. import ERROR, OK, WARNING

LINE = re.compile(r"^(?P<pat>[a-z0-9_]+\.[a-z0-9_*?]+)\s+(?P<op>is\s+not|is|below|above)\s+(?P<val>.+?)\s+"
                  r"(?P<lvl>warning|error)$", re.I)
LEVELS = {"warning": WARNING, "error": ERROR}
NOT_READY = ("unavailable", "unknown")
MAX_RULES = 200


@dataclass(frozen=True)
class Rule:
    line: int
    pattern: str
    op: str  # is | is not | below | above
    value: str
    level: int

    def matches(self, entity_id: str) -> bool:
        return fnmatchcase(entity_id, self.pattern)

    def text(self) -> str:
        return f"{self.op} {self.value}"


def parse(text: str) -> tuple[list[Rule], str]:
    """Rules from the settings text; error names the first bad line."""
    rules = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            return [], (f"Entity rules, line {n}: write <entity or pattern> is|is not|below|above <value> "
                        f"warning|error, e.g. sensor.oil_level below 200 warning")
        op = " ".join(m["op"].lower().split())
        val = m["val"].strip()
        if op in ("below", "above"):
            try:
                float(val)
            except ValueError:
                return [], f"Entity rules, line {n}: '{op}' needs a number, not {val}"
        rules.append(Rule(n, m["pat"].lower(), op, val, LEVELS[m["lvl"].lower()]))
    if len(rules) > MAX_RULES:
        return [], f"Entity rules: at most {MAX_RULES} rules"
    return rules, ""


def wanted(rules: list[Rule], entity_id: str) -> bool:
    return any(r.matches(entity_id) for r in rules)


def _hit(rule: Rule, state) -> bool | None:
    """True when the rule fires; None when a number rule has no number to compare."""
    st = str(state if state is not None else "unknown")
    if rule.op == "is":
        return st.lower() == rule.value.lower()
    if rule.op == "is not":
        return st.lower() != rule.value.lower()
    try:
        v = float(st)
    except ValueError:
        return None
    return v < float(rule.value) if rule.op == "below" else v > float(rule.value)


def evaluate(rules: list[Rule], entities: list[dict]) -> tuple[int, str, int]:
    """(status, detail, number of entities firing) over the entities the rules match."""
    if not rules:
        return OK, "no rules (add them in the settings)", 0
    worst: dict[str, tuple[int, str]] = {}  # entity -> (level, text); one line per entity, its worst rule
    problems = []
    seen = set()
    for r in rules:
        mine = [e for e in entities if r.matches(e["entity_id"])]
        if not mine:
            problems.append(f"line {r.line}: no entity matches {r.pattern}")
            continue
        for e in mine:
            seen.add(e["entity_id"])
            name = e.get("name") or e["entity_id"]
            unit = f" {e['unit']}" if e.get("unit") else ""
            hit = _hit(r, e.get("state"))
            if hit is None:
                problems.append(f"{name}: no reading ({e.get('state')})")
            elif hit and r.level > worst.get(e["entity_id"], (OK, ""))[0]:
                worst[e["entity_id"]] = (r.level, f"{name} {e.get('state')}{unit} ({r.text()})")
    firing = sorted(worst.values(), key=lambda x: -x[0])
    status = max([lv for lv, _ in firing] + [WARNING if problems else OK])
    parts = [t for _, t in firing] + problems
    if not parts:
        return OK, f"{len(rules)} rules, {len(seen)} entities fine", 0
    return status, "; ".join(parts), len(firing)
