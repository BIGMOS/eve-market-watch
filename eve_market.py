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

import requests

ESI = "https://esi.evetech.net/latest"
UA = "eve-market-watch/1.0 (personal trading tool)"

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


def weighted_avg(hist, days):
    """Volume-weighted average price over the last N days of history."""
    tail = hist[-days:] if hist else []
    if not tail:
        return None, 0, 0
    vol = sum(d["volume"] for d in tail)
    if vol == 0:
        return sum(d["average"] for d in tail) / len(tail), 0, len(tail)
    wa = sum(d["average"] * d["volume"] for d in tail) / vol
    return wa, vol / len(tail), len(tail)


def analyse(item, region_id, station_id, fees, defaults):
    tid = item["type_id"]
    orders = fetch_orders(region_id, tid)
    hist = fetch_history(region_id, tid)

    scope = [o for o in orders if o["location_id"] == station_id] if station_id else orders

    sells = sorted([o for o in scope if not o["is_buy_order"]], key=lambda o: o["price"])
    buys = sorted([o for o in scope if o["is_buy_order"]], key=lambda o: -o["price"])

    best_sell = sells[0]["price"] if sells else None
    best_buy = buys[0]["price"] if buys else None

    avg7, vol7, _ = weighted_avg(hist, 7)
    avg30, vol30, n30 = weighted_avg(hist, 30)
    anchor = avg7 or avg30          # what the item is "really" worth right now
    daily_vol = vol7 or vol30 or 0

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
        """Units listed cheaper than `price` -- the queue in front of you."""
        return sum(o["volume_remain"] for o in sells if o["price"] < price)

    wall_5pct = sum(o["volume_remain"] for o in sells
                    if best_sell and o["price"] <= best_sell * 1.05)
    sellers_5pct = sum(1 for o in sells
                       if best_sell and o["price"] <= best_sell * 1.05)
    total_listed = sum(o["volume_remain"] for o in sells)

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
        days_to_clear = (below / daily_vol) if daily_vol else float("inf")
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
        days = ((ahead + qty) / daily_vol) if daily_vol else float("inf")
        why.append(f"Beats the {sellers_5pct} order(s) within 5% of top "
                   f"({wall_5pct:,.0f} units).")
        why.append(f"Expected clear time for your {qty:,.0f}: {fmt_days(days)}.")
        if anchor and price < anchor * 0.85:
            why.append(f"WARNING: market looks dumped - 7d average is {isk(anchor)}, "
                       f"{(1 - price / anchor) * 100:.0f}% above this price. "
                       f"Consider parking at {isk(anchor)} and waiting.")
        if anchor and price > anchor * 1.25:
            why.append(f"Top of book is {(price / anchor - 1) * 100:.0f}% over the 7d "
                       f"average - thin book, this price may not hold.")

    net = price * keep if price else None
    profit_each = (net - cost) if (net is not None and cost) else None
    margin = (profit_each / cost * 100) if (profit_each is not None and cost) else None

    ahead_final = ahead_of(price) if price else 0
    days_final = ((ahead_final + qty) / daily_vol) if daily_vol else float("inf")

    buy_net = best_buy * keep_buyorder if best_buy else None
    spread = ((best_sell - best_buy) / best_sell * 100) if (best_sell and best_buy) else None

    return {
        "name": item["name"], "type_id": tid, "qty": qty, "cost": cost,
        "best_sell": best_sell, "best_buy": best_buy, "spread_pct": spread,
        "avg7": avg7, "avg30": avg30, "daily_vol": daily_vol,
        "listed": total_listed, "sellers_5pct": sellers_5pct, "wall_5pct": wall_5pct,
        "breakeven": breakeven, "floor": floor,
        "price": price, "net_each": net, "profit_each": profit_each,
        "margin_pct": margin,
        "profit_total": profit_each * qty if (profit_each is not None) else None,
        "days_to_sell": days_final, "ahead": ahead_final,
        "buyorder_net": buy_net,
        "rec": rec, "why": why, "history_days": n30,
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
            f"{pct(r['margin_pct']):>8}{fmt_days(r['days_to_sell']):>10}  {r['rec']}"
        )
    out += ["-" * w,
            "  * sell time assumes you stay the cheapest order. In a hub you will be",
            "    undercut within minutes, so treat it as a best case, not a promise.",
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
    ap.add_argument("--watchlist", default="watchlist.json")
    ap.add_argument("--hub", default=None, choices=sorted(HUBS))
    ap.add_argument("--region-wide", action="store_true",
                    help="analyse the whole region instead of a single station")
    ap.add_argument("--json", metavar="FILE", help="also write raw results as JSON")
    ap.add_argument("--html", metavar="FILE", help="also write an HTML report")
    args = ap.parse_args()

    with open(args.watchlist, encoding="utf-8") as f:
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
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(lambda it: analyse(it, region_id, station_id, fees, defaults),
                           items))

    rows.sort(key=lambda r: -(r["profit_total"] or 0))
    print(table(rows, loc, fees))

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
