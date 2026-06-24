#!/usr/bin/env python3
"""Phase-1 passive liquidity-rewards measurement harness for Polymarket.
Ranks reward pools by gross reward-yield-per-in-band-dollar (the uncrowded mid-tail signal).
No capital, no order signing — pure live-API measurement. Adverse-selection bleed needs the
time-series mode (run repeatedly); this pass computes the gross/capacity side decisively.
"""
import json, subprocess, time, statistics, sys

def curl(u, timeout=60):
    r = subprocess.run(["curl","-s","--socks5-hostname","127.0.0.1:7890",u],
                       capture_output=True, text=True, timeout=timeout)
    try: return json.loads(r.stdout)
    except: return None

def pool_score(book_side, mid, max_spread, is_bid):
    """PM-style in-band score: sum size * ((c - s)/c)^2 for orders within max_spread (in cents) of mid."""
    c = max_spread  # cents
    tot_notional = 0.0
    score = 0.0
    for lvl in book_side:
        price = float(lvl["price"]); size = float(lvl["size"])
        s = (mid - price) if is_bid else (price - mid)   # distance from mid, in price units
        s_cents = s * 100.0
        if s_cents <= c + 1e-9 and s_cents >= -1e-9:
            w = ((c - s_cents)/c)**2 if c > 0 else 0
            score += size * w
            tot_notional += size * (price if is_bid else (1-price))  # collateral-ish notional
    return score, tot_notional

print("Pulling reward pools from CLOB /sampling-markets ...")
d = curl("https://clob.polymarket.com/sampling-markets")
data = d.get("data", d) if isinstance(d, dict) else d
pools = []
for m in data:
    rw = m.get("rewards") or {}
    rates = rw.get("rates") or []
    daily = sum(float(rt.get("rewards_daily_rate",0) or 0) for rt in rates)
    if daily < 50:        # focus on tradeable pools, skip $1 dust
        continue
    toks = m.get("tokens") or []
    tok = None
    for t in toks:
        if t.get("token_id"): tok = t["token_id"]; break
    pools.append({
        "daily": daily,
        "max_spread": float(rw.get("max_spread", m.get("rewards",{}).get("max_spread",0)) or 0),
        "min_size": float(rw.get("min_size",0) or 0),
        "tick": float(m.get("minimum_tick_size",0.01) or 0.01),
        "q": (m.get("question","")[:46]),
        "cid": m.get("condition_id",""),
        "token": tok,
    })
print(f"pools with daily_rate>=$50: {len(pools)}  (total ${sum(p['daily'] for p in pools):,.0f}/day)")

# fetch book per pool, compute in-band notional + my-share if I post min_size two-sided
results = []
for i,p in enumerate(pools):
    if not p["token"]: continue
    bk = curl(f"https://clob.polymarket.com/book?token_id={p['token']}")
    if not bk: continue
    bids = bk.get("bids") or []; asks = bk.get("asks") or []
    if not bids or not asks: continue
    best_bid = max(float(b["price"]) for b in bids)
    best_ask = min(float(a["price"]) for a in asks)
    mid = (best_bid+best_ask)/2
    c = p["max_spread"]
    bscore,bnot = pool_score(bids, mid, c, True)
    ascore,anot = pool_score(asks, mid, c, False)
    inband_notional = bnot + anot
    # my two-sided min_size quote one tick inside band: spread ~ tick from mid -> near-max weight
    s_cents = p["tick"]*100
    myw = ((c - s_cents)/c)**2 if c>0 else 0
    my_score_side = p["min_size"]*myw
    # two-sided qualifying score ~ min(bid,ask) side; my contribution roughly my_score_side on the lighter side
    existing_min_side = min(bscore, ascore)
    my_share = my_score_side / (my_score_side + existing_min_side) if (my_score_side+existing_min_side)>0 else 0
    my_capital = p["min_size"]*mid + p["min_size"]*(1-mid)   # collateral for two-sided min_size
    my_daily_reward = my_share * p["daily"]
    ann_yield = (my_daily_reward*365)/my_capital if my_capital>0 else 0
    results.append({**{k:p[k] for k in ("q","daily","max_spread","min_size","tick","cid")},
        "mid":round(mid,3),"spread_c":round((best_ask-best_bid)*100,2),
        "inband_notional":round(inband_notional),"my_share":round(my_share,4),
        "my_daily_reward":round(my_daily_reward,2),"my_capital":round(my_capital,1),
        "ann_yield_pct":round(ann_yield*100,1)})
    time.sleep(0.05)

results.sort(key=lambda r:-r["ann_yield_pct"])
json.dump(results, open("/tmp/lp_pools_ranked.json","w"), indent=2)
print(f"\nanalyzed {len(results)} pools with live books")
print("\n=== TOP 25 by gross annualized reward yield (posting min_size two-sided, one tick in-band) ===")
print(f"{'ann%':>7} {'$/day':>7} {'mySh':>6} {'inbandNot':>10} {'cap$':>7} {'sprd':>5} {'mid':>5}  market")
for r in results[:25]:
    print(f"{r['ann_yield_pct']:>7.0f} {r['daily']:>7.0f} {r['my_share']:>6.2f} {r['inband_notional']:>10,} {r['my_capital']:>7.0f} {r['spread_c']:>5.1f} {r['mid']:>5.2f}  {r['q']}")
print("\n=== summary ===")
ys=[r["ann_yield_pct"] for r in results]
print(f"median gross ann yield: {statistics.median(ys):.0f}% | pools >100%/yr: {sum(1 for y in ys if y>100)} | >15%/yr: {sum(1 for y in ys if y>15)}")
print("NOTE: this is GROSS (pre adverse-selection). Net requires the time-series pickoff measurement.")
