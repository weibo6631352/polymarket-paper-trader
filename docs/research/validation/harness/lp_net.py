#!/usr/bin/env python3
"""Net-yield analyzer: combines reward accrual (gross) with measured adverse-selection bleed
from /tmp/lp_timeseries.jsonl to rank PM reward pools by NET annualized yield.

Bleed model: each interval where mid moves >= 1 tick, a two-sided min_size maker gets the
stale side picked off -> buys/sells min_size at an off-market price, loss ~ min_size * |move|.
Net daily = gross_reward_share*pool_daily  -  pickoffs_per_day * min_size * avg_adverse_move.
"""
import json, statistics
from collections import defaultdict

rows = [json.loads(l) for l in open("/tmp/lp_timeseries.jsonl")]
by = defaultdict(list)
for r in rows:
    by[r["tok"]].append(r)

# join real reward-share computed cross-sectionally by the harness (keyed by question)
try:
    harness = {h["q"]: h for h in json.load(open("/tmp/lp_pools_ranked.json"))}
except Exception:
    harness = {}

INTERVAL_SEC = 60
intervals_per_day = 86400 / INTERVAL_SEC

out = []
for tok, rs in by.items():
    rs.sort(key=lambda r: r["rnd"])
    moves = [r for r in rs if "dmid_c" in r]
    if len(moves) < 5:    # need enough samples
        continue
    n = len(moves)
    pickoffs = [r for r in moves if r.get("pickoff")]
    pick_rate = len(pickoffs)/n
    adverse_moves = [abs(r["dmid_c"])/100 for r in pickoffs]   # price units
    avg_adv = statistics.mean(adverse_moves) if adverse_moves else 0
    realized_vol_c = statistics.pstdev([r["dmid_c"] for r in moves]) if n>1 else 0
    g = rs[-1]
    mid = statistics.mean(r["mid"] for r in rs)
    tick = g["tick"]; min_size = g["min_size"]; daily = g["daily"]; c = g["max_spread"]
    # gross reward share: assume my min_size one tick in-band vs typical in-band competition.
    # Use a conservative fixed competing in-band score proxy = larger of (observed nothing) -> here
    # we don't re-pull books; approximate share by min_size weight vs a nominal pool of 5000 shares.
    s_cents = tick*100
    myw = ((c - s_cents)/c)**2 if c>0 else 0
    typ_spread = statistics.median(r["spread_c"] for r in rs)
    # prefer the harness-measured reward share (min_size two-sided one tick in-band vs existing in-band score);
    # fall back to band-empty heuristic only if harness data missing for this market.
    h = harness.get(g["q"])
    if h and "my_share" in h:
        share = h["my_share"]
    else:
        share = 1.0 if typ_spread > c else 0.05
    gross_daily = share * daily
    capital = min_size*mid + min_size*(1-mid)
    # bleed: pickoffs per day * min_size * avg adverse move (loss when stale side filled)
    bleed_daily = pick_rate * intervals_per_day * min_size * avg_adv
    net_daily = gross_daily - bleed_daily
    net_ann = (net_daily*365)/capital*100 if capital>0 else 0
    gross_ann = (gross_daily*365)/capital*100 if capital>0 else 0
    out.append({"q":g["q"],"daily":daily,"share":share,"typ_spread_c":round(typ_spread,2),
        "max_spread_c":c,"pick_rate":round(pick_rate,3),"avg_adv_c":round(avg_adv*100,2),
        "vol_c":round(realized_vol_c,2),"capital":round(capital,1),
        "gross_ann":round(gross_ann),"bleed_daily":round(bleed_daily,2),
        "net_daily":round(net_daily,2),"net_ann":round(net_ann),"n":n})

out.sort(key=lambda r:-r["net_ann"])
json.dump(out, open("/tmp/lp_net_ranked.json","w"), indent=2)
print(f"pools with >=5 move-samples: {len(out)} | sampling rounds available: {max((r['rnd'] for r in rows), default=0)+1}")
print(f"\n{'netAnn%':>8} {'grossAnn%':>9} {'$/day':>6} {'shr':>4} {'pickRt':>6} {'advC':>5} {'volC':>5} {'tSpC':>5} {'mxSpC':>5}  market")
for r in out[:30]:
    print(f"{r['net_ann']:>8,} {r['gross_ann']:>9,} {r['daily']:>6.0f} {r['share']:>4.2f} {r['pick_rate']:>6.2f} {r['avg_adv_c']:>5.1f} {r['vol_c']:>5.1f} {r['typ_spread_c']:>5.1f} {r['max_spread_c']:>5.1f}  {r['q']}")
pos=[r for r in out if r["net_ann"]>15]
print(f"\npools NET >15%/yr: {len(pos)} / {len(out)}  | median net: {statistics.median([r['net_ann'] for r in out]) if out else 0:.0f}%/yr")
print("CONFIRM edge if a stable mid-tail subset holds net>15%/yr across the full sample.")
