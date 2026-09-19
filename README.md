# EVE Market Watch

Reads live order books and price history from ESI (public, no login or API key)
and tells you what to list your products at.

It is built for sellers rather than station traders: it weighs the order book
against what an item actually costs *you*, and will tell you not to undercut
when undercutting means selling at a loss.

## Requirements

Python 3.8+ and `requests`:

```bash
pip install requests
```

No API key, no login, no ESI scopes - all market data used here is public.

## Paste what you're selling

```bash
python server.py
```

Opens a page at `http://127.0.0.1:8765` with a box to paste into. One item per
line, or select your hangar and copy straight out of the client. Hit **Price it**
(or Ctrl+Enter) and you get the exact price to list each item at - click any
price to copy it. **Save to watchlist** folds what you pasted into
`watchlist.json` so it's there next time.

Accepted formats, mixed freely:

```
Damage Control II                 one unit
Hobgoblin II 500                  quantity after the name
Warrior II x500                   x prefix
425mm AutoCannon II	60	Module   tab-separated hangar copy
Caracal x15 @ 8400000             @ what it costs you -> margins too
```

Leave the `@cost` off and it reuses whatever cost is already stored for that
item in your watchlist. The page binds to localhost only - nothing is exposed
to the network.

## Or run it from the terminal

```bash
python eve_market.py                      # Jita 4-4, console table
python eve_market.py --html report.html   # + HTML dashboard
python eve_market.py --hub amarr          # jita | amarr | dodixie | rens | hek
python eve_market.py --region-wide        # whole region, not just the hub station
python eve_market.py --json snapshot.json # raw numbers for your own analysis
```

Needs Python 3 and `requests` (`pip install requests`).

## Edit `watchlist.json` — it's the only file you maintain

First run:

```bash
cp watchlist.example.json watchlist.json
```

`watchlist.json` is gitignored — your costs, quantities and margins stay on your
machine and never reach the repo.

```json
{ "name": "Damage Control II", "qty": 120, "cost": 296000, "min_margin": 20 }
```

- `name` — exact in-game spelling; the type ID is resolved for you.
- `qty` — how many you have to sell.
- `cost` — what ONE unit costs you to build or buy. Leave `null` and you still
  get market data, but no margin, no floor and no HOLD warnings.
- `min_margin` — optional per-item override of `defaults.min_margin` (percent).

### Two settings that decide everything

**`items[].cost`** — the shipped values are placeholders at 78% of each item's
7-day average, put there only to demo both code paths. Replace them.

**`fees`** — defaults are broker `1.5` / sales tax `3.6`, which assume
Broker Relations V and Accounting V. With no skills it's `3.0` / `8.0`, and that
moves your break-even by roughly 6% of revenue. Standings cut the broker fee
further.

## How the price is chosen

1. **Break-even** = `cost / (1 - broker - tax)` — a sell order pays both fees.
2. **Floor** = break-even x (1 + min_margin). You never list below this.
3. **Undercut** = best sell - 0.01.
4. If undercut >= floor -> **UNDERCUT**. Otherwise -> **HOLD** at your floor,
   with a count of the units listed below it and how long that stock takes to
   clear at current volume.
5. If even matching the cheapest order won't return your cost -> **UNDERWATER**.
   It says so plainly, shows the per-unit loss and what cutting losses now would
   net, and excludes the item from the projected-profit totals rather than
   quoting a price the market will not pay.
6. Selling into a buy order pays sales tax but *no* broker fee, so the
   instant-sale net is always shown as the alternative.
7. If the top of book is more than 15% under the 7-day volume-weighted average,
   the market is being dumped and you get a warning instead of a race to the
   bottom.

## Station-only vs whole region

By default the tool only counts orders at the hub station itself. Tick **whole
region** (or pass `--region-wide`) and it counts every station and structure in
that region instead.

This matters more than it sounds. Same item, same moment, in Metropolis:

| | Best sell | Margin | Competition within 5% |
|---|---|---|---|
| Hek station only | 444.30k | 42.4% | 7 orders, 979 units |
| Whole region | 405.50k | 30.0% | 3 orders, 144 units |

Region-wide drags your suggested price down to match someone selling elsewhere
in the region - 12 points of margin. When it does, it now tells you where they
are:

> Region-wide: the cheapest order (405.50k, 96 units within 2% of it) is at
> Tratokard II - Moon 1 - CONCORD Bureau. If you are not selling there, that
> order is only competing with you if buyers will travel.

So you can judge it yourself. Buyers shop at hubs; an order 12 jumps out in a
quiet system is usually not taking your sale, and matching it just gives away
margin. Player structures show as "a player structure in <system>" - naming them
needs an authenticated ESI scope this tool does not use.

## Caveats

- **"Sells in" assumes you stay the cheapest order.** In a trade hub you'll be
  undercut within minutes. Use it to rank items by liquidity, not as a delivery
  date.
- Margins are only as honest as the `cost` numbers you put in.
- Buy-order analysis looks at orders at the station; a regional buy order with a
  wide range that you could also sell into may not be counted.
- ESI market data is cached around 5 minutes, so re-running faster than that
  returns the same book.

## Files

| File | Purpose |
|---|---|
| `eve_market.py` | fetching, analysis, console report |
| `server.py` | the paste-and-price page |
| `paste.py` | parses pasted sell lists |
| `report.py` | HTML dashboard renderer |
| `watchlist.json` | your items, costs, fees, hub |
| `snapshot.json` | last run's raw numbers (regenerated, safe to delete) |

## License

MIT - see [LICENSE](LICENSE). Not affiliated with or endorsed by CCP Games.
EVE Online and all related assets are the property of CCP hf.
