"""Local paste-and-price page.

    python server.py            -> http://127.0.0.1:8765

Paste what you want to sell, get the exact price to list each item at.
Binds to localhost by default; pass --host 0.0.0.0 to serve it to your LAN.
"""

import argparse
import html
import json
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import eve_market
import paste
import report
from eve_market import (HUBS, analyse, ensure_watchlist, load_bookstate,
                        resolve_names, save_bookstate)

# Beside the script rather than in the cwd; main() re-points it for --watchlist.
WATCHLIST = eve_market.WATCHLIST

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EVE Market Watch</title><style>{css}
.panel{{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  padding:16px;margin-bottom:24px}}
textarea{{
  width:100%;min-height:150px;resize:vertical;padding:12px;border-radius:8px;
  border:1px solid var(--axis);background:var(--plane);color:var(--ink);
  font:13.5px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;
}}
textarea:focus{{outline:2px solid var(--bar);outline-offset:-1px;border-color:transparent}}
.controls{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}}
select,button{{
  font:14px system-ui,-apple-system,"Segoe UI",sans-serif;border-radius:8px;
  border:1px solid var(--axis);padding:8px 12px;background:var(--surface);color:var(--ink);
}}
button{{cursor:pointer;font-weight:600}}
button.primary{{background:var(--bar);border-color:var(--bar);color:#fff;padding:8px 18px}}
button.primary:hover{{filter:brightness(1.08)}}
button:disabled{{opacity:.55;cursor:default}}
label.chk{{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--ink-2)}}
.hint{{font-size:12px;color:var(--muted);margin:10px 0 0;line-height:1.6}}
.hint code{{
  background:color-mix(in srgb,var(--muted) 16%,transparent);
  padding:1px 5px;border-radius:4px;font-size:11.5px;
}}
.msg{{margin-top:12px;font-size:13px}}
.msg.err{{color:var(--critical)}} .msg.ok{{color:var(--good-ink)}}
.spin{{display:inline-block;width:13px;height:13px;border:2px solid var(--bar-soft);
  border-top-color:var(--bar);border-radius:50%;animation:s .7s linear infinite;
  vertical-align:-2px;margin-right:7px}}
@keyframes s{{to{{transform:rotate(360deg)}}}}
</style></head>
<body><div class="wrap">
<h1>What are you selling?</h1>
<p class="sub">Paste your list &mdash; one item per line, or copy straight out of your
hangar. You get the exact price to list each one at.</p>

<div class="panel">
  <textarea id="txt" spellcheck="false" placeholder="Damage Control II x120
Hobgoblin II 500
Caracal x15 @ 8400000"></textarea>
  <div class="controls">
    <button class="primary" id="go">Price it</button>
    <select id="hub">{hubs}</select>
    <label class="chk"><input type="checkbox" id="wide"> whole region</label>
    <button id="save" disabled>Save to watchlist</button>
  </div>
  <p class="hint">
    <code>Item Name</code> &middot; <code>Item Name 120</code> &middot;
    <code>Item Name x120</code> &middot; tab-separated hangar copy &middot;
    add <code>@296000</code> for what it costs you, and you get margins too.
  </p>
  <div class="msg" id="msg"></div>
</div>

<div id="out"></div>
</div>
<script>
{copy_js}
var txt=document.getElementById('txt'), go=document.getElementById('go'),
    save=document.getElementById('save'), msg=document.getElementById('msg'),
    out=document.getElementById('out'), hub=document.getElementById('hub'),
    wide=document.getElementById('wide'), lastItems=null;

function post(url,body){{
  return fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify(body)}}).then(function(r){{return r.json();}});
}}

function price(){{
  var t=txt.value.trim();
  if(!t){{ msg.className='msg err'; msg.textContent='Paste something first.'; return; }}
  go.disabled=true; save.disabled=true;
  msg.className='msg'; msg.innerHTML='<span class="spin"></span>Reading the order books\\u2026';
  post('/api/analyse',{{text:t,hub:hub.value,region_wide:wide.checked}})
   .then(function(d){{
     go.disabled=false;
     if(d.error){{ msg.className='msg err'; msg.textContent=d.error; return; }}
     out.innerHTML=d.html; lastItems=d.items; save.disabled=false;
     msg.className='msg'+(d.missing.length?' err':'');
     msg.textContent=d.missing.length
       ? 'Not found (check the in-game spelling): '+d.missing.join(', ')
       : d.items.length+' item(s) priced.';
   }})
   .catch(function(e){{ go.disabled=false; msg.className='msg err';
     msg.textContent='Failed: '+e; }});
}}

go.addEventListener('click',price);
txt.addEventListener('keydown',function(e){{
  if((e.ctrlKey||e.metaKey)&&e.key==='Enter') price();
}});
save.addEventListener('click',function(){{
  if(!lastItems) return;
  save.disabled=true;
  post('/api/save',{{items:lastItems,hub:hub.value}}).then(function(d){{
    msg.className='msg '+(d.error?'err':'ok');
    msg.textContent=d.error||('Saved \\u2014 watchlist now holds '+d.count+' items.');
  }});
}});
</script></body></html>"""


def build_page():
    hubs = "".join(f'<option value="{k}"{" selected" if k == "jita" else ""}>'
                   f'{html.escape(v["label"])}</option>' for k, v in HUBS.items())
    return PAGE.format(css=report.CSS, copy_js=report.COPY_JS, hubs=hubs)


def load_cfg():
    try:
        with open(WATCHLIST, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def do_analyse(body):
    cfg = load_cfg()
    fees = cfg.get("fees", {"broker_pct": 1.5, "sales_tax_pct": 3.6})
    defaults = cfg.get("defaults", {"min_margin": 15, "tick": 0.01})

    items = paste.parse(body.get("text", ""))
    if not items:
        return {"error": "Nothing recognisable in that paste."}

    # fall back to the cost basis already stored for an item, if any
    known = {i["name"].lower(): i.get("cost") for i in cfg.get("items", [])}
    for it in items:
        if it["cost"] is None:
            it["cost"] = known.get(it["name"].lower())

    ids = resolve_names([i["name"] for i in items])
    missing = []
    for it in items:
        tid = ids.get(it["name"].lower())
        if tid:
            it["type_id"] = tid
        else:
            missing.append(it["name"])
    items = [i for i in items if "type_id" in i]
    if not items:
        return {"error": "No item names matched: " + ", ".join(missing)}

    hub = body.get("hub", "jita")
    if hub not in HUBS:
        hub = "jita"
    region = HUBS[hub]["region"]
    station = None if body.get("region_wide") else HUBS[hub]["station"]

    # same book-diff measurement the CLI does, so both surfaces learn the real
    # station fill rate and share one bookstate.json
    state = load_bookstate()
    scope = str(station) if station else f"region{region}"
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(
            lambda it: analyse(it, region, station, fees, defaults,
                               state.get(f"{it['type_id']}@{scope}"), now),
            items))
    for r in rows:
        state[f"{r['type_id']}@{scope}"] = r.pop("_state")
    save_bookstate(state)
    rows.sort(key=lambda r: -((r["price"] or 0) * r["qty"]))

    return {
        "missing": missing,
        "items": [{"name": r["name"], "qty": r["qty"], "cost": r["cost"]} for r in rows],
        "html": (f'<div class="tiles">{report.render_tiles(rows, fees)}</div>'
                 f'{report.render_table(rows)}{report.FOOT}'),
    }


def do_save(body):
    cfg = load_cfg()
    if not cfg:
        return {"error": f"Could not read {WATCHLIST}."}
    cfg["hub"] = body.get("hub", cfg.get("hub", "jita"))
    by_name = {i["name"].lower(): i for i in cfg.setdefault("items", [])}
    for it in body.get("items", []):
        key = it["name"].lower()
        if key in by_name:
            by_name[key]["qty"] = it["qty"]
            if it.get("cost") is not None:
                by_name[key]["cost"] = it["cost"]
        else:
            cfg["items"].append({"name": it["name"], "qty": it["qty"],
                                 "cost": it.get("cost")})
    with open(WATCHLIST, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return {"count": len(cfg["items"])}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            self._send(200, build_page(), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        route = {"/api/analyse": do_analyse, "/api/save": do_save}.get(self.path)
        if not route:
            self._send(404, '{"error":"no such endpoint"}', "application/json")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            result = route(body)
        except Exception as e:                      # surface it in the page
            result = {"error": f"{type(e).__name__}: {e}"}
        self._send(200, json.dumps(result), "application/json; charset=utf-8")


def main():
    ap = argparse.ArgumentParser(description="Local paste-and-price page")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind; 0.0.0.0 exposes the page to your "
                         "whole network (the page has no login, so only do this "
                         "on a network you trust)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--watchlist", default=None,
                    help="watchlist file (default: watchlist.json beside this script, "
                         "created from watchlist.example.json on first run)")
    args = ap.parse_args()

    global WATCHLIST
    WATCHLIST = ensure_watchlist(args.watchlist)

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}"
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    where = "all interfaces" if args.host == "0.0.0.0" else args.host
    print(f"Market watch running at {url}   (bound to {where}, ctrl-c to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
