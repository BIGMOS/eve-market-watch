"""Renders market-watch results as HTML.

`write_html` produces the standalone report file; `render_tiles` / `render_table`
are shared with the paste server so both surfaces look and behave the same.
"""

import html
from datetime import datetime, timezone

CSS = """
:root{
  color-scheme:light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,0.10);
  --bar:#2a78d6; --bar-soft:#cde2fb;
  --good:#0ca30c; --warning:#fab219; --critical:#d03b3b;
  --good-ink:#006300;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,0.10);
    --bar:#3987e5; --bar-soft:#184f95;
    --good-ink:#0ca30c;
  }
}
*{box-sizing:border-box}
body{
  margin:0; padding:32px 16px 64px; background:var(--plane); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-0.01em}
.sub{color:var(--ink-2);font-size:13px;margin:0 0 24px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:28px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:14px 16px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.tile .v{font-size:26px;font-weight:600;margin-top:6px;letter-spacing:-0.02em}
.tile .n{font-size:12px;color:var(--ink-2);margin-top:4px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{
  text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);font-weight:600;padding:12px 12px 10px;border-bottom:1px solid var(--grid);
  white-space:nowrap;
}
th.l,td.l{text-align:left}
td{padding:11px 12px;border-bottom:1px solid var(--grid);text-align:right;
   font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:color-mix(in srgb,var(--bar) 6%,transparent)}
.name{font-weight:550;letter-spacing:-0.005em}
.tid{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.listat{font-weight:650;font-size:14px;cursor:pointer;position:relative}
.listat:hover{color:var(--bar)}
.listat::after{
  content:"copy"; position:absolute; right:12px; top:50%; transform:translateY(-50%);
  font-size:9px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
  opacity:0; transition:opacity .12s;
}
.listat:hover::after{opacity:1}
.listat.copied::after{content:"copied"; opacity:1; color:var(--good-ink)}
.pos{color:var(--good-ink)} .neg{color:var(--critical)}
.badge{
  display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:600;
  padding:3px 8px;border-radius:999px;border:1px solid var(--ring);
}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.b-sell{color:var(--good-ink)} .b-hold{color:var(--warning)}
.b-open{color:var(--bar)} .b-under{color:var(--critical)}
.barcell{width:150px}
.bar{height:8px;background:var(--bar-soft);border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--bar);border-radius:4px}
.why{padding:0 12px 14px;margin:0;font-size:12.5px;color:var(--ink-2)}
.why li{margin:3px 0}
details summary{
  cursor:pointer;padding:9px 12px;font-size:12px;color:var(--ink-2);
  list-style:none;border-bottom:1px solid var(--grid)
}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"\\25B8 ";color:var(--muted)}
details[open] summary::before{content:"\\25BE "}
.foot{margin-top:18px;font-size:12px;color:var(--muted);line-height:1.6}
@media(max-width:760px){
  .barcell,.colhide{display:none}
  body{padding:20px 12px 48px}
  table{font-size:12.5px} td,th{padding:9px 8px}
}
"""

COPY_JS = """
document.addEventListener('click', function(e){
  var c = e.target.closest('.listat'); if(!c || !c.dataset.copy) return;
  navigator.clipboard.writeText(c.dataset.copy).then(function(){
    c.classList.add('copied');
    setTimeout(function(){ c.classList.remove('copied'); }, 1100);
  });
});
"""


def isk(v):
    if v is None:
        return "—"
    a = abs(v)
    for div, suf in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if a >= div:
            return f"{v/div:,.2f}{suf}"
    return f"{v:,.2f}"


def fmt_days(d):
    if d is None or d == float("inf"):
        return "—"
    if d * 1440 < 90:
        return f"{d*1440:.0f}min"
    if d < 1:
        return f"{d*24:.1f}h"
    return ">1y" if d > 365 else f"{d:.1f}d"


def badge(rec):
    cls = ("b-under" if rec == "UNDERWATER" else
           "b-hold" if rec.startswith("HOLD") else
           "b-open" if "OPEN" in rec else "b-sell")
    return f'<span class="badge {cls}">{html.escape(rec)}</span>'


def render_tiles(rows, fees):
    sellable = [r for r in rows if r["rec"] != "UNDERWATER"]
    patient = sum(r["profit_total"] for r in sellable if r["profit_total"])
    liquid = sum((r["buyorder_net"] - r["cost"]) * r["qty"]
                 for r in rows if r["buyorder_net"] and r["cost"])
    stock = sum((r["price"] or 0) * r["qty"] for r in sellable)
    held = sum(1 for r in rows if r["rec"].startswith("HOLD"))
    under = sum(1 for r in rows if r["rec"] == "UNDERWATER")
    priced = sum(1 for r in rows if r["cost"])

    tiles = [("Stock at suggested prices", isk(stock),
              f"{len(sellable)} items" + (f", {under} underwater excluded" if under else ""))]
    if priced:
        tiles += [
            ("Profit — patient", isk(patient), "sell orders, if they all fill"),
            ("Profit — liquidate now", isk(liquid), "dumped into buy orders"),
        ]
    else:
        tiles += [("Margin", "—", "add costs to see profit")]
    tiles.append(("Needs patience", f"{held}", f"of {len(rows)} items say HOLD"))
    if under:
        tiles.append(("Underwater", f"{under}",
                      "cost above what the market pays"))
    return "".join(
        f'<div class="tile"><div class="k">{html.escape(k)}</div>'
        f'<div class="v">{v}</div><div class="n">{html.escape(n)}</div></div>'
        for k, v, n in tiles)


def render_table(rows):
    peak = max((abs(r["profit_total"] or 0) for r in rows), default=0) or 1
    trs = []
    for r in rows:
        p = r["profit_total"] or 0
        w = min(100, abs(p) / peak * 100)
        margin = (f'<span class="{"pos" if r["margin_pct"] >= 0 else "neg"}">'
                  f'{r["margin_pct"]:.1f}%</span>') if r["margin_pct"] is not None else "—"
        why = "".join(f"<li>{html.escape(x)}</li>" for x in r["why"])
        price = f"{r['price']:,.2f}" if r["price"] else "—"
        copy = f"{r['price']:.2f}" if r["price"] else ""
        trs.append(f"""
        <tr>
          <td class="l"><div class="name">{html.escape(r['name'])}</div>
              <div class="tid">type {r['type_id']} &middot; {r['qty']:,} on hand</div></td>
          <td class="colhide">{isk(r['best_buy'])}</td>
          <td>{isk(r['best_sell'])}</td>
          <td class="listat" data-copy="{copy}" title="click to copy">{price}</td>
          <td class="colhide">{isk(r['net_each'])}</td>
          <td>{margin}</td>
          <td class="colhide">{fmt_days(r['days_to_sell'])}</td>
          <td class="barcell"><div class="bar" title="{isk(p)} ISK at stake">
              <i style="width:{w:.1f}%"></i></div></td>
          <td class="l">{badge(r['rec'])}</td>
        </tr>
        <tr><td class="l" colspan="9" style="padding:0;border-bottom:1px solid var(--grid)">
            <details><summary>why this price</summary>
            <ul class="why">{why}</ul></details></td></tr>""")

    return f"""<div class="card"><table>
<thead><tr>
  <th class="l">Item</th><th class="colhide">Best buy</th><th>Best sell</th>
  <th>List at</th><th class="colhide">Net each</th><th>Margin</th>
  <th class="colhide">Sells in</th><th class="barcell">ISK at stake</th>
  <th class="l">Action</th>
</tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>"""


FOOT = """<p class="foot">
<strong>List at</strong> is the exact price to enter in the client &mdash; click it to copy.
<strong>Net each</strong> is what lands in your wallet after broker fee and sales tax.
<strong>Sells in</strong> assumes you stay the cheapest order &mdash; in a trade hub you will be
undercut within minutes, so read it as a best case.<br>
Margins are computed against the cost basis you supplied; they are only as
honest as those numbers.</p>"""


def write_html(path, rows, loc, fees):
    keep = 1 - (fees["broker_pct"] + fees["sales_tax_pct"]) / 100
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EVE Market Watch</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>Market watch &mdash; {html.escape(loc)}</h1>
<p class="sub">{datetime.now(timezone.utc):%d %b %Y, %H:%M} UTC &middot; live ESI order books &middot;
broker {fees['broker_pct']}% + sales tax {fees['sales_tax_pct']}% &rarr; you keep
{keep*100:.2f}% of a sell-order price</p>
<div class="tiles">{render_tiles(rows, fees)}</div>
{render_table(rows)}
{FOOT}
</div><script>{COPY_JS}</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
