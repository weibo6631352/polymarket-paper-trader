#!/usr/bin/env python3
"""Jump-risk filter for PM reward pools (workflow Rank-2 KILL rule).
For each candidate pool, pull CLOB price history, measure max single-day jump and daily vol.
KILL rule: if one historical max daily jump (in $ P&L on min_size) wipes > 20 days of reward
accrual, the pool is a deferred-jump trap regardless of low minute-vol. Survivors = the
genuinely steady mid-tail worth quoting."""
import json, subprocess, statistics, time

def curl(u, t=40):
    try:
        r = subprocess.run(["curl","-s","--socks5-hostname","127.0.0.1:7890",u],
                           capture_output=True, text=True, timeout=t); return json.loads(r.stdout)
    except: return None

# candidate pools = harness ranked (has q, daily, min_size, max_spread) ; need token -> re-pull sampling
d = curl("https://clob.polymarket.com/sampling-markets")
data = d.get("data", d) if isinstance(d, dict) else d
tokmap = {}
for m in (data or []):
    rw = m.get("rewards") or {}
    daily = sum(float(rt.get("rewards_daily_rate",0) or 0) for rt in (rw.get("rates") or []))
    if daily < 50: continue
    tok = next((t.get("token_id") for t in (m.get("tokens") or []) if t.get("token_id")), None)
    if tok: tokmap[m.get("question","")[:46]] = (tok, daily, float(rw.get("min_size",0) or 0))

try: ranked = json.load(open("/tmp/lp_pools_ranked.json"))
except: ranked = [{"q":q} for q in tokmap]

res = []
for r in ranked:
    q = r["q"]
    if q not in tokmap: continue
    tok, daily, min_size = tokmap[q]
    h = curl(f"https://clob.polymarket.com/prices-history?market={tok}&interval=max&fidelity=1440")
    pts = (h or {}).get("history") or []
    if len(pts) < 10:
        res.append({"q":q,"daily":daily,"days":len(pts),"verdict":"no-history"}); continue
    prices = [float(p["p"]) for p in pts]
    daily_moves = [abs(prices[i]-prices[i-1]) for i in range(1,len(prices))]
    max_jump = max(daily_moves)
    vol = statistics.pstdev(daily_moves)
    # reward/day on min_size two-sided (assume share from harness if present else 0.05)
    share = r.get("my_share", 0.05)
    rew_day = share*daily
    # loss from a max-jump pickoff ~ min_size * max_jump (stale side filled, unwind at new price)
    jump_loss = min_size * max_jump
    days_wiped = jump_loss/rew_day if rew_day>0 else 9999
    verdict = "KILL" if days_wiped > 20 else ("WATCH" if days_wiped > 7 else "SAFE")
    res.append({"q":q,"daily":daily,"min_size":min_size,"days":len(pts),
        "max_jump_c":round(max_jump*100,1),"daily_vol_c":round(vol*100,2),
        "rew_day":round(rew_day,2),"jump_loss":round(jump_loss,1),
        "days_wiped":round(days_wiped,1),"share":round(share,3),"verdict":verdict})
    time.sleep(0.05)

order = {"SAFE":0,"WATCH":1,"KILL":2,"no-history":3}
res.sort(key=lambda r:(order.get(r.get("verdict"),9), -r.get("daily",0)))
json.dump(res, open("/tmp/lp_jumprisk.json","w"), indent=2)
safe = [r for r in res if r.get("verdict")=="SAFE"]
print(f"analyzed {len(res)} pools | SAFE {sum(1 for r in res if r.get('verdict')=='SAFE')} | WATCH {sum(1 for r in res if r.get('verdict')=='WATCH')} | KILL {sum(1 for r in res if r.get('verdict')=='KILL')}")
print(f"\n{'verdict':>8} {'$/day':>6} {'maxJmpC':>7} {'volC':>5} {'rew/d':>6} {'daysWiped':>9}  market")
for r in res:
    if r.get("verdict") in ("SAFE","WATCH"):
        print(f"{r['verdict']:>8} {r['daily']:>6.0f} {r['max_jump_c']:>7.1f} {r['daily_vol_c']:>5.1f} {r['rew_day']:>6.2f} {r['days_wiped']:>9.1f}  {r['q']}")
print("\n--- a few KILLs for contrast ---")
for r in [x for x in res if x.get('verdict')=='KILL'][:8]:
    print(f"{r['verdict']:>8} {r['daily']:>6.0f} {r['max_jump_c']:>7.1f} maxjump wipes {r['days_wiped']:.0f}d  {r['q']}")
