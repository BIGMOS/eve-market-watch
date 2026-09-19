"""Parses pasted sell lists into watchlist items.

Accepts the formats you actually end up with:

    Damage Control II                 -> qty 1
    Damage Control II 120             -> qty 120
    Damage Control II x120            -> qty 120
    Damage Control II    120    ...   -> EVE inventory copy (tab separated)
    Damage Control II x120 @296000    -> qty 120, cost basis 296,000 each

Blank lines and obvious header rows are ignored. The same item pasted twice
has its quantities added together.
"""

import re

_COST = re.compile(r"@\s*([\d,.]+)\s*$")
_TRAILING_QTY = re.compile(r"\s+[xX]?\s*([\d,]+)$")
_INT = re.compile(r"\d+")
_HEADERS = {"name", "item", "item name", "quantity", "qty", "type"}


def _num(s):
    try:
        return float(s.replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def parse_line(line):
    """-> {"name", "qty", "cost"} or None if the line carries nothing."""
    line = line.strip().lstrip("-*• ").strip()
    if not line:
        return None

    cost = None
    m = _COST.search(line)
    if m:
        cost = _num(m.group(1))
        line = line[:m.start()].strip()

    # EVE's inventory copy is tab separated: name, qty, group, category, ...
    parts = [p.strip() for p in line.split("\t")] if "\t" in line else [line]
    name = parts[0].strip()
    if not name or name.lower() in _HEADERS:
        return None

    qty = None
    for p in parts[1:]:
        cell = p.strip().lstrip("xX").replace(",", "").strip()
        if cell and _INT.fullmatch(cell):
            qty = int(cell)
            break

    if qty is None:
        m = _TRAILING_QTY.search(name)
        if m:
            qty = int(m.group(1).replace(",", ""))
            name = name[:m.start()].strip()

    if not name:
        return None
    return {"name": name, "qty": qty if qty is not None else 1, "cost": cost}


def parse(text):
    """Parse a whole pasted block. Duplicate names are merged."""
    merged = {}
    order = []
    for raw in (text or "").splitlines():
        it = parse_line(raw)
        if not it:
            continue
        key = it["name"].lower()
        if key in merged:
            merged[key]["qty"] += it["qty"]
            if it["cost"] is not None:
                merged[key]["cost"] = it["cost"]
        else:
            merged[key] = it
            order.append(key)
    return [merged[k] for k in order]
