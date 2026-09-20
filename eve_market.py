#!/usr/bin/env python3
"""
EVE Online market watch + sell-price advisor.

Pulls live order books and price history from ESI (public, no auth needed),
then recommends a sell price for each item on your watchlist based on
competition, your cost basis, fees, and how fast the item actually moves.

Usage:
    python eve_market.py                      # Jita 4-4, uses watchlist.json
    python eve_market.py --hub amarr
    python eve_market.py --region-wide        # whole region, not one station
    python eve_market.py --html report.html
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

ESI = "https://esi.evetech.net/latest"
UA = "eve-market-watch/1.0 (personal trading tool)"

# Anchored to this file rather than the working directory, so the tool runs the
# same from a shell in the repo, a desktop shortcut, or another drive.
HERE = Path(__file__).resolve().parent
WATCHLIST = HERE / "watchlist.json"
EXAMPLE = HERE / "watchlist.example.json"


def ensure_watchlist(path=None):
    """Return the watchlist path, seeding it from the example on first run.

    This replaces the manual copy step, which had no cross-platform spelling
    (`cp` on Linux/macOS, `copy` on Windows). Clone and run; your costs still
    live in the gitignored watchlist.json, never in the example.
    """
    target = Path(path) if path else WATCHLIST
    if not target.exists():
        if not EXAMPLE.exists():
            sys.exit(f"No watchlist at {target} and no {EXAMPLE.name} to seed it from.")
        target.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Created {target} from {EXAMPLE.name} - "
              f"edit it to add your own costs.", file=sys.stderr)
    return target


HUBS = {
    "jita":    {"region": 10000002, "station": 60003760, "label": "Jita IV-4 (The Forge)"},
    "amarr":   {"region": 10000043, "station": 60008494, "label": "Amarr VIII (Domain)"},
    "dodixie": {"region": 10000032, "station": 60011866, "label": "Dodixie IX-20 (Sinq Laison)"},
    "rens":    {"region": 10000030, "station": 60004588, "label": "Rens VI-8 (Heimatar)"},
    "hek":     {"region": 10000042, "station": 60005686, "label": "Hek VIII-12 (Metropolis)"},
}

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "application/json"})


def get(path, **params):
    params.setdefault("datasource", "tranquility")
    r = S.get(f"{ESI}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r


def resolve_names(names):
    """Map item names -> type_ids via ESI (exact, case-insensitive match)."""
    out = {}
    for i in range(0, len(names), 500):
        chunk = names[i:i + 500]
        r = S.post(f"{ESI}/universe/ids/", params={"datasource": "tranquility"},
                   json=chunk, timeout=30)
        r.raise_for_status()
        for t in r.json().get("inventory_types", []):
            out[t["name"].lower()] = t["id"]
    return out


def fetch_orders(region_id, type_id):
    """All buy+sell orders for one type in a region (handles pagination)."""
    orders, page = [], 1
    while True:
        r = get(f"/markets/{region_id}/orders/", type_id=type_id,
                order_type="all", page=page)
        batch = r.json()
        orders.extend(batch)
        pages = int(r.headers.get("X-Pages", 1))
        if page >= pages or not batch:
            break
        page += 1
    return orders


def fetch_history(region_id, type_id):
    try:
        return get(f"/markets/{region_id}/history/", type_id=type_id).json()
    except requests.HTTPError:
        return []


_LOC_CACHE = {}


def location_name(location_id, system_id):
    """Human name for where an order sits.

    NPC stations resolve by id. Player structures need an authenticated scope
    we do not have, so fall back to the solar system, which is always public.
    """
    if location_id in _LOC_CACHE:
        return _LOC_CACHE[location_id]

    name = None
    if location_id < 100_000_000:                      # NPC station range
        try:
            name = get(f"/universe/stations/{location_id}/").json().get("name")
        except requests.HTTPError:
            name = None
    if not name and system_id:
        try:
            sys_name = get(f"/universe/systems/{system_id}/").json().get("name")
            name = f"a player structure in {sys_name}"
        except requests.HTTPError:
            name = None

    _LOC_CACHE[location_id] = name or "an unnamed location"
    return _LOC_CACHE[location_id]


def median(values):
    v = sorted(values)
    n = len(v)
    if not n:
        return 0
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


BOOKSTATE = HERE / "bookstate.json"
MIN_OBS_HOURS = 0.25        # ESI caches market data ~5 min; shorter gaps are noise
MIN_TRUST_HOURS = 2.0       # total observation needed before the measured rate is used
OBS_RETENTION_DAYS = 14
SNAPSHOT_DEPTH = 300        # cheapest N sell orders worth tracking


def load_bookstate():
    try:
        return json.loads(BOOKSTATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_bookstate(state):
    try:
        BOOKSTATE.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        print(f"!! could not write {BOOKSTATE.name}: {e}", file=sys.stderr)


def measure_fill_rate(prior, sells, now):
    """Units/day actually sold AT THIS STATION, measured by diffing order books.

    ESI publishes trade history per REGION only. In The Forge that figure is
    dominated by Perimeter and the trade-hub structures, so dividing a Jita 4-4
    queue by it badly overstates how fast an order fills: a 25,236-unit wall sat
    untouched for 20 hours while regional history claimed 12,357 units/day.

    So measure it instead. Units missing from an order we saw last run were
    bought here -- that is a real trade at this station. Orders that vanished
    entirely are ambiguous (filled, cancelled or expired), so they are recorded
    but kept OUT of the rate. The result is a deliberate lower bound, not a
    guess: it is the volume we can prove traded.

    Returns (units_per_day or None, hours_observed, n_samples, new_state).
    """
    snapshot = {"ts": now.isoformat(),
                "orders": {str(o["order_id"]): o["volume_remain"]
                           for o in sells[:SNAPSHOT_DEPTH]}}
    observations = list((prior or {}).get("observations", []))
    last = (prior or {}).get("snapshot")

    if last:
        try:
            hours = (now - datetime.fromisoformat(last["ts"])).total_seconds() / 3600.0
        except (ValueError, KeyError, TypeError):
            hours = 0.0
        if hours >= MIN_OBS_HOURS:
            before, after = last.get("orders", {}), snapshot["orders"]
            sold = sum(max(0, before[o] - after[o]) for o in before.keys() & after.keys())
            gone = sum(before[o] for o in before.keys() - after.keys())
            observations.append({"ts": now.isoformat(), "hours": round(hours, 3),
                                 "sold": sold, "vanished": gone})

    cutoff = now.timestamp() - OBS_RETENTION_DAYS * 86400
    kept = []
    for o in observations:
        try:
            if datetime.fromisoformat(o["ts"]).timestamp() >= cutoff:
                kept.append(o)
        except (ValueError, KeyError, TypeError):
            pass
    kept = kept[-50:]

    total_h = sum(o["hours"] for o in kept)
    total_sold = sum(o["sold"] for o in kept)
    # A single short sample proves nothing: over ten minutes almost everything
    # looks stalled. Keep deferring to regional history until we have enough
    # wall-clock coverage for "nothing sold" to be a real finding.
    rate = (total_sold / total_h * 24) if total_h >= MIN_TRUST_HOURS else None
    return rate, total_h, len(kept), {"snapshot": snapshot, "observations": kept}


def weighted_avg(hist, days):
    """Volume-weighted average price, and MEDIAN daily volume, over N days.

    The volume figure is a median rather than a mean on purpose. Ore and
    mineral books get occasional one-day spikes - a single 44k-unit day in an
    otherwise 12k week dragged the mean up 35% and made every "sells in"
    estimate that used it optimistic. The median ignores the spike.

    The price average stays volume-weighted: for price, a heavy day genuinely
    should count for more.
    """
    tail = hist[-days:] if hist else []
    if not tail:
        return None, 0, 0
    vol = sum(d["volume"] for d in tail)
    med_vol = median([d["volume"] for d in tail])
    if vol == 0:
        return sum(d["average"] for d in tail) / len(tail), 0, len(tail)
    wa = sum(d["average"] * d["volume"] for d in tail) / vol
    return wa, med_vol, len(tail)


def analyse(item, region_id, station_id, fees, defaults, prior=None, now=None):
    now = now or datetime.now(timezone.utc)
    tid = item["type_id"]
    orders = fetch_orders(region_id, tid)
    hist = fetch_history(region_id, tid)

    scope = [o for o in orders if o["location_id"] == station_id] if station_id else orders

    sells = sorted([o for o in scope if not o["is_buy_order"]], key=lambda o: o["price"])
    buys = sorted([o for o in scope if o["is_buy_order"]], key=lambda o: -o["price"])

    best_sell = sells[0]["price"] if sells else None
    best_buy = buys[0]["price"] if buys else None

    # only meaningful region-wide; at a single station everything is there
    cheapest_at = None
    if sells and station_id is None:
        cheapest_at = location_name(sells[0]["location_id"], sells[0].get("system_id"))

    avg7, vol7, _ = weighted_avg(hist, 7)
    avg30, vol30, n30 = weighted_avg(hist, 30)
    anchor = avg7 or avg30          # what the item is "really" worth right now
    daily_vol = vol7 or vol30 or 0                      # REGIONAL, from ESI history

    # Prefer throughput we measured at this station over regional history.
    observed, obs_hours, obs_n, newstate = measure_fill_rate(prior, sells, now)
    fill_rate = observed if observed is not None else daily_vol
    fill_source = "station" if observed is not None else "region"

    # --- fees -------------------------------------------------------------
    broker = fees["broker_pct"] / 100.0
    tax = fees["sales_tax_pct"] / 100.0
    keep = 1.0 - broker - tax        # fraction of a sell-order price you keep
    keep_buyorder = 1.0 - tax        # selling INTO a buy order: tax only, no broker fee

    cost = item.get("cost")
    min_margin = item.get("min_margin", defaults["min_margin"]) / 100.0
    qty = item.get("qty", 0)
    tick = item.get("tick", defaults["tick"])

    breakeven = (cost / keep) if cost else None
    floor = breakeven * (1 + min_margin) if breakeven else None

    # --- competition ------------------------------------------------------
    undercut = round(best_sell - tick, 2) if best_sell else None

    def ahead_of(price):
        """Units that fill before yours -- the queue in front of you.

        Includes orders at exactly `price`. EVE fills same-price orders
        oldest-first, so stock already resting at your price is ahead of you,
        not beside you. Listing on a round number that other sellers have
        already crowded onto can put tens of thousands of units in front.
        """
        return sum(o["volume_remain"] for o in sells if o["price"] <= price)

    wall_5pct = sum(o["volume_remain"] for o in sells
                    if best_sell and o["price"] <= best_sell * 1.05)
    sellers_5pct = sum(1 for o in sells
                       if best_sell and o["price"] <= best_sell * 1.05)
    total_listed = sum(o["volume_remain"] for o in sells)
    # stock resting ON the top price. Listing at that round number instead of a
    # tick under queues you behind every unit of it.
    at_top = sum(o["volume_remain"] for o in sells
                 if best_sell and o["price"] == best_sell)

    # --- decide -----------------------------------------------------------
    why = []

    if not sells:
        price = round(anchor * 1.10, 2) if anchor else (round(floor, 2) if floor else None)
        rec = "OPEN MARKET"
        why.append("No competing sell orders here - you set the price.")
        if anchor:
            why.append(f"Priced 10% over the 7d regional average of {isk(anchor)}.")
    elif breakeven and breakeven > best_sell:
        # even matching the cheapest order does not return your cost
        price = round(floor, 2)
        rec = "UNDERWATER"
        loss = cost - best_sell * keep
        why.append(f"You cannot break even here: matching the best sell of "
                   f"{isk(best_sell)} nets {isk(best_sell * keep)} against a "
                   f"{isk(cost)} cost - a {isk(loss)} loss per unit.")
        if anchor and anchor * keep < cost:
            why.append(f"The 7d average of {isk(anchor)} is below your cost too, so "
                       f"this is not a dip you can wait out at this hub.")
        else:
            why.append(f"The 7d average of {isk(anchor)} would clear your cost - "
                       f"the current book is depressed, so waiting may recover it.")
        if best_buy:
            why.append(f"Cutting losses now nets {isk(best_buy * keep_buyorder)}/unit "
                       f"({isk(cost - best_buy * keep_buyorder)} down per unit).")
        why.append(f"Listed price shown is your {min_margin*100:.0f}% floor, not a "
                   f"price this market will pay today.")
    elif floor and undercut < floor:
        # undercutting would lose money: wait for the cheap stock to burn off
        below = ahead_of(floor)
        days_to_clear = (below / fill_rate) if fill_rate else float("inf")
        price = round(floor, 2)
        if best_buy and best_buy * keep_buyorder > cost:
            rec = "HOLD / or dump to buy"
            why.append(f"Buy order at {isk(best_buy)} still nets "
                       f"{isk(best_buy * keep_buyorder)}/unit vs {isk(cost)} cost.")
        else:
            rec = "HOLD - do not undercut"
        why.append(f"Undercutting to {isk(undercut)} is below your "
                   f"{min_margin * 100:.0f}% floor of {isk(floor)}.")
        why.append(f"{below:,.0f} units are listed under your floor "
                   f"({fmt_days(days_to_clear)} of volume to clear).")
    else:
        price = undercut
        rec = "UNDERCUT"
        ahead = ahead_of(price)
        days = ((ahead + qty) / fill_rate) if fill_rate else float("inf")
        why.append(f"Beats the {sellers_5pct} order(s) within 5% of top "
                   f"({wall_5pct:,.0f} units).")
        why.append(f"Expected clear time for your {qty:,.0f}: {fmt_days(days)}.")
        if at_top:
            queued = ((at_top + qty) / fill_rate) if fill_rate else float("inf")
            why.append(f"List at {exact(price)}, NOT {exact(best_sell)}: "
                       f"{at_top:,.0f} units already rest on {exact(best_sell)}, and "
                       f"same-price orders fill oldest-first. The round number puts "
                       f"you behind all of them - {fmt_days(queued)} instead of "
                       f"{fmt_days(days)}.")
        if anchor and price < anchor * 0.85:
            why.append(f"WARNING: market looks dumped - 7d average is {isk(anchor)}, "
                       f"{(1 - price / anchor) * 100:.0f}% above this price. "
                       f"Consider parking at {isk(anchor)} and waiting.")
        if anchor and price > anchor * 1.25:
            why.append(f"Top of book is {(price / anchor - 1) * 100:.0f}% over the 7d "
                       f"average - thin book, this price may not hold.")

    if fill_source == "station" and not fill_rate:
        why.append(f"STALLED: nothing at all has traded here across {obs_hours:.1f}h of "
                   f"measurement ({obs_n} sample(s)), despite regional history claiming "
                   f"{daily_vol:,.0f} units/day. The regional figure is counting trade at "
                   f"other stations and structures, not this one.")
    elif fill_source == "station":
        why.append(f"Timing uses {fill_rate:,.0f} units/day measured AT THIS STATION "
                   f"({obs_hours:.1f}h of order-book diffs, {obs_n} sample(s)), not the "
                   f"{daily_vol:,.0f}/day regional history.")
    else:
        why.append(f"No station data yet, so timing falls back to {daily_vol:,.0f}/day "
                   f"REGIONAL history. That counts trade at every station and structure "
                   f"in the region, so it overstates one station - treat the estimate as "
                   f"unverified. Re-run in {MIN_OBS_HOURS * 60:.0f}+ minutes to start "
                   f"measuring the real rate here.")

    if cheapest_at:
        undercut_ahead = sum(o["volume_remain"] for o in sells
                             if o["location_id"] == sells[0]["location_id"]
                             and o["price"] <= best_sell * 1.02)
        why.append(f"Region-wide: the cheapest order ({isk(best_sell)}, "
                   f"{undercut_ahead:,.0f} units within 2% of it) is at "
                   f"{cheapest_at}. If you are not selling there, that order is "
                   f"only competing with you if buyers will travel.")

    net = price * keep if price else None
    profit_each = (net - cost) if (net is not None and cost) else None
    margin = (profit_each / cost * 100) if (profit_each is not None and cost) else None

    ahead_final = ahead_of(price) if price else 0
    days_final = ((ahead_final + qty) / fill_rate) if fill_rate else float("inf")

    buy_net = best_buy * keep_buyorder if best_buy else None
    spread = ((best_sell - best_buy) / best_sell * 100) if (best_sell and best_buy) else None

    return {
        "name": item["name"], "type_id": tid, "qty": qty, "cost": cost,
        "best_sell": best_sell, "best_buy": best_buy, "spread_pct": spread,
        "avg7": avg7, "avg30": avg30, "daily_vol": daily_vol,
        "listed": total_listed, "sellers_5pct": sellers_5pct, "wall_5pct": wall_5pct,
        "at_top": at_top,
        "breakeven": breakeven, "floor": floor,
        "price": price, "net_each": net, "profit_each": profit_each,
        "margin_pct": margin,
        "profit_total": profit_each * qty if (profit_each is not None) else None,
        "days_to_sell": days_final, "ahead": ahead_final,
        "buyorder_net": buy_net,
        "fill_rate": fill_rate, "fill_source": fill_source,
        "obs_hours": obs_hours, "obs_n": obs_n, "_state": newstate,
        "rec": rec, "why": why, "history_days": n30,
        "cheapest_at": cheapest_at,
    }


# ---------- formatting -------------------------------------------------------

def isk(v):
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:,.2f}b"
    if a >= 1e6:
        return f"{v / 1e6:,.2f}m"
    if a >= 1e3:
        return f"{v / 1e3:,.2f}k"
    return f"{v:,.2f}"


def exact(v):
    """Full-precision price - this is the number you paste into the client."""
    return f"{v:,.2f}" if v is not None else "-"


def fmt_days(d):
    if d is None or d == float("inf"):
        return "no data"
    if d * 24 * 60 < 90:
        return f"{d * 24 * 60:.0f}min"
    if d < 1:
        return f"{d * 24:.1f}h"
    if d > 365:
        return ">1y"
    return f"{d:.1f}d"


def pct(v):
    return f"{v:.1f}%" if v is not None else "-"


def sells_in(r):
    """Fill time. Trailing ? = unverified regional history rather than measured.

    "stalled" is different from "no data": it means we watched this book for
    hours and saw nothing trade here at all.
    """
    if r.get("fill_source") == "station" and not r.get("fill_rate"):
        return "stalled"
    t = fmt_days(r["days_to_sell"])
    return t if r.get("fill_source") == "station" else t + "?"


def table(rows, loc, fees):
    w = 118
    keep_pct = 100 - fees["broker_pct"] - fees["sales_tax_pct"]
    out = [
        "=" * w,
        f"  EVE MARKET WATCH   {loc}   {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        f"  Fees: broker {fees['broker_pct']}% + sales tax {fees['sales_tax_pct']}%"
        f"  ->  you keep {keep_pct:.2f}% of a sell-order price",
        "=" * w, "",
        f"{'ITEM':<29}{'QTY':>7}{'BEST SELL':>12}{'LIST AT':>17}"
        f"{'NET/EA':>12}{'MARGIN':>8}{'SELLS IN*':>10}  ACTION",
        "-" * w,
    ]
    for r in rows:
        out.append(
            f"{r['name'][:28]:<29}{r['qty']:>7,}{isk(r['best_sell']):>12}"
            f"{exact(r['price']):>17}{isk(r['net_each']):>12}"
            f"{pct(r['margin_pct']):>8}{sells_in(r):>10}  {r['rec']}"
        )
    out += ["-" * w,
            "  * sell time assumes you stay the cheapest order -- in a hub you will be",
            "    undercut within minutes, so treat it as a best case, not a promise.",
            "    A trailing ? means the rate is REGIONAL history, which counts every",
            "    station in the region and overstates this one. No ? means the rate was",
            "    measured here by diffing order books between runs.",
            ""]

    tp = sum(r["profit_total"] for r in rows
             if r["profit_total"] and r["rec"] != "UNDERWATER")
    liq = sum((r["buyorder_net"] - r["cost"]) * r["qty"] for r in rows
              if r["buyorder_net"] and r["cost"])
    held = sum(1 for r in rows if r["rec"].startswith("HOLD"))
    under = sum(1 for r in rows if r["rec"] == "UNDERWATER")
    out.append(f"  Profit, patient   (sell orders at the suggested prices): {isk(tp)} ISK")
    if liq:
        out.append(f"  Profit, liquidate (dump everything to buy orders now): {isk(liq)} ISK")
    if held:
        out.append(f"  {held} of {len(rows)} items say HOLD - the patient number "
                   f"depends on those actually selling.")
    if under:
        out.append(f"  {under} item(s) are UNDERWATER (cost above market) and are "
                   f"excluded from the patient number.")
    out += ["", "  DETAIL", "  " + "-" * (w - 2)]

    for r in rows:
        out.append(f"  {r['name']}  (type {r['type_id']})")
        book = f"    book      best sell {isk(r['best_sell'])} | best buy {isk(r['best_buy'])}"
        if r["spread_pct"] is not None:
            book += f" | spread {r['spread_pct']:.1f}%"
        out.append(book)
        out.append(f"    history   7d avg {isk(r['avg7'])} | 30d avg {isk(r['avg30'])}"
                   f" | {r['daily_vol']:,.0f} units/day")
        out.append(f"    supply    {r['listed']:,.0f} listed here | "
                   f"{r['sellers_5pct']} order(s) within 5% of top | "
                   f"{r['ahead']:,.0f} units ahead of you at the suggested price")
        if r.get("at_top"):
            out.append(f"    queue     {r['at_top']:,.0f} units resting on "
                       f"{exact(r['best_sell'])} itself - undercut it, do not match it")
        if r.get("fill_source") == "station":
            out.append(f"    measured  {r['fill_rate']:,.0f} units/day actually sold here "
                       f"({r['obs_hours']:.1f}h of book diffs, {r['obs_n']} sample(s)) "
                       f"vs {r['daily_vol']:,.0f}/day regional")
        if r["cost"]:
            out.append(f"    yours     cost {isk(r['cost'])} -> breakeven "
                       f"{isk(r['breakeven'])} | floor {isk(r['floor'])}")
        if r["buyorder_net"]:
            out.append(f"    instant   dumping to the buy order nets "
                       f"{isk(r['buyorder_net'])}/unit, sells immediately")
        for line in r["why"]:
            out.append(f"    * {line}")
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="EVE market watch / sell price advisor")
    ap.add_argument("--watchlist", default=None,
                    help="watchlist file (default: watchlist.json beside this script, "
                         "created from watchlist.example.json on first run)")
    ap.add_argument("--hub", default=None, choices=sorted(HUBS))
    ap.add_argument("--region-wide", action="store_true",
                    help="analyse the whole region instead of a single station")
    ap.add_argument("--json", metavar="FILE", help="also write raw results as JSON")
    ap.add_argument("--html", metavar="FILE", help="also write an HTML report")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the table; for scheduled runs whose only job is to "
                         "feed the fill-rate measurement")
    ap.add_argument("--log", metavar="FILE",
                    help="append a one-line summary of each run to FILE")
    args = ap.parse_args()

    with open(ensure_watchlist(args.watchlist), encoding="utf-8") as f:
        cfg = json.load(f)

    hub = args.hub or cfg.get("hub", "jita")
    region_id = HUBS[hub]["region"]
    station_id = None if (args.region_wide or cfg.get("region_wide")) else HUBS[hub]["station"]
    loc = HUBS[hub]["label"] + ("  [region-wide]" if station_id is None else "")

    fees = cfg.get("fees", {"broker_pct": 1.5, "sales_tax_pct": 3.6})
    defaults = cfg.get("defaults", {"min_margin": 15, "tick": 0.01})

    items = cfg["items"]
    unresolved = [i["name"] for i in items if "type_id" not in i]
    if unresolved:
        print(f"Resolving {len(unresolved)} item name(s)...", file=sys.stderr)
        ids = resolve_names(unresolved)
        missing = []
        for i in items:
            if "type_id" not in i:
                tid = ids.get(i["name"].lower())
                if tid:
                    i["type_id"] = tid
                else:
                    missing.append(i["name"])
        if missing:
            print(f"!! Not found (check exact in-game spelling): {', '.join(missing)}",
                  file=sys.stderr)
            items = [i for i in items if "type_id" in i]

    print(f"Fetching order books for {len(items)} item(s) at {loc}...", file=sys.stderr)
    # Loaded before the pool and only read inside it, so the threads share nothing
    # mutable; each row hands its new snapshot back and main() persists them here.
    state = load_bookstate()
    scope = str(station_id) if station_id else f"region{region_id}"
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(
            lambda it: analyse(it, region_id, station_id, fees, defaults,
                               state.get(f"{it['type_id']}@{scope}"), now),
            items))

    for r in rows:
        state[f"{r['type_id']}@{scope}"] = r.pop("_state")
    save_bookstate(state)

    measured = sum(1 for r in rows if r["fill_source"] == "station")
    if measured < len(rows):
        print(f"note: {len(rows) - measured} of {len(rows)} item(s) have no station "
              f"fill data yet (marked ? below). Re-run later to build it.",
              file=sys.stderr)

    rows.sort(key=lambda r: -(r["profit_total"] or 0))
    if not args.quiet:
        print(table(rows, loc, fees))

    if args.log:
        try:
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(f"{now:%Y-%m-%d %H:%M UTC}  {loc}  {len(rows)} item(s)  "
                        f"{measured} measured / {len(rows) - measured} regional\n")
        except OSError as e:
            print(f"!! could not write {args.log}: {e}", file=sys.stderr)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"location": loc, "fees": fees,
                       "generated": datetime.now(timezone.utc).isoformat(),
                       "rows": rows}, f, indent=2)
        print(f"wrote {args.json}", file=sys.stderr)

    if args.html:
        from report import write_html
        write_html(args.html, rows, loc, fees)
        print(f"wrote {args.html}", file=sys.stderr)


if __name__ == "__main__":
    main()
