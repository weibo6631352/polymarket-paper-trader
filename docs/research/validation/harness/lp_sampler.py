#!/usr/bin/env python3
"""Adverse-selection time-series sampler for PM reward pools (Phase-1 part 2).
Polls candidate pools' books every INTERVAL sec for ROUNDS rounds, recording mid moves.
A mid move >= 1 tick against a resting quote = a pickoff event (the bleed term).
Appends one JSON line per (round, pool) to /tmp/lp_timeseries.jsonl."""
import json, subprocess, time

INTERVAL = 60
ROUNDS = 90          # ~90 min of sampling
def curl(u, t=30):
    try:
        r = subprocess.run(["curl","-s","--socks5-hostname","127.0.0.1:7890",u],
                           capture_output=True, text=True, timeout=t)
        return json.loads(r.stdout)
    except: return None

# build candidate set: reward pools daily>=50, take up to 45 with the smallest in-band books (highest gross)
d = curl("https://clob.polymarket.com/sampling-markets")
data = d.get("data", d) if isinstance(d, dict) else d
cands = []
for m in (data or []):
    rw = m.get("rewards") or {}
    daily = sum(float(rt.get("rewards_daily_rate",0) or 0) for rt in (rw.get("rates") or []))
    if daily < 50: continue
    tok = next((t.get("token_id") for t in (m.get("tokens") or []) if t.get("token_id")), None)
    if not tok: continue
    cands.append({"token":tok,"daily":daily,"max_spread":float(rw.get("max_spread",0) or 0),
                  "min_size":float(rw.get("min_size",0) or 0),
                  "tick":float(m.get("minimum_tick_size",0.01) or 0.01),
                  "q":m.get("question","")[:46]})
cands = cands[:55]
f = open("/tmp/lp_timeseries.jsonl","a")
prev = {}
for rnd in range(ROUNDS):
    ts = int(time.time())
    for c in cands:
        bk = curl(f"https://clob.polymarket.com/book?token_id={c['token']}")
        if not bk: continue
        bids = bk.get("bids") or []; asks = bk.get("asks") or []
        if not bids or not asks: continue
        bb = max(float(b["price"]) for b in bids); ba = min(float(a["price"]) for a in asks)
        mid = (bb+ba)/2
        rec = {"t":ts,"rnd":rnd,"tok":c["token"][:12],"q":c["q"],"daily":c["daily"],
               "tick":c["tick"],"max_spread":c["max_spread"],"min_size":c["min_size"],
               "bb":bb,"ba":ba,"mid":round(mid,4),"spread_c":round((ba-bb)*100,2)}
        if c["token"] in prev:
            dmid = mid - prev[c["token"]]
            rec["dmid_c"] = round(dmid*100,3)
            rec["pickoff"] = abs(dmid) >= c["tick"]   # moved >=1 tick => stale side would be picked off
        prev[c["token"]] = mid
        f.write(json.dumps(rec)+"\n")
    f.flush()
    time.sleep(INTERVAL)
f.close()
print("sampling complete", ROUNDS, "rounds", len(cands), "pools")
