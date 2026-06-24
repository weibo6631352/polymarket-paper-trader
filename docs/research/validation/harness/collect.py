#!/usr/bin/env python3
"""Collect (forecast_price, realized_outcome) pairs from resolved Polymarket binary markets.

Gamma /markets offset pagination (cap 2500, ~100/page) with server-side volume_num_min
to focus on liquid markets. Parallel CLOB price-history fetches.
"""
from __future__ import annotations
import json, subprocess, time, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

PROXY = ["--socks5-hostname", "127.0.0.1:7890"]
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/prices-history"

def curl(url: str, tries: int = 5) -> str | None:
    for i in range(tries):
        try:
            r = subprocess.run(["curl", "-s", "--max-time", "30", *PROXY, url],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except Exception:
            pass
        time.sleep(0.6 * (i + 1))
    return None

def clean_binary(m: dict) -> int | None:
    try:
        op = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"]
        oc = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    except Exception:
        return None
    if not op or len(op) != 2 or len(oc) != 2:
        return None
    a, b = float(op[0]), float(op[1])
    if {round(a), round(b)} != {0, 1} or abs(a - round(a)) > 1e-6 or abs(b - round(b)) > 1e-6:
        return None
    return 1 if round(a) == 1 else 0

def categorize(q: str, slug: str) -> str:
    s = (q + " " + slug).lower()
    crypto_kw = ["bitcoin","btc","ethereum","eth","solana","sol","crypto","fdv","token",
                 "airdrop","coin","price above","price below","hit $","reach $","up or down",
                 "dogecoin","xrp","cardano","memecoin","binance"]
    pol_kw = ["election","president","senate","congress","governor","poll","vote","trump",
              "biden","harris","democrat","republican","parliament","prime minister","fed ",
              "interest rate","government shutdown","supreme court","nominee","cabinet","gop",
              "putin","zelensky","ukraine","israel","gaza","tariff","impeach","mamdani","nyc mayor"]
    sports_kw = ["nba","nfl","mlb","nhl","soccer","premier league","champions league",
                 "la liga","serie a","bundesliga","ucl","epl","vs.","vs ","match","wimbledon",
                 "fight","ufc","boxing","tennis","cup","f1","grand prix","atp","wta","open:",
                 "playoff","finals","super bowl","world series","golf","cricket","o/u"]
    weather_kw = ["temperature","highest temp","°c","°f","rain ","snow "]
    if any(k in s for k in crypto_kw): return "crypto"
    if any(k in s for k in pol_kw): return "politics"
    if any(k in s for k in sports_kw): return "sports"
    if any(k in s for k in weather_kw): return "weather"
    return "other"

def price_at_lead(history: list, close_ts: int, lead_secs: int) -> float | None:
    target = close_ts - lead_secs
    best = None
    for pt in history:
        t = pt.get("t"); p = pt.get("p")
        if t is None or p is None: continue
        if t <= target: best = p
        else: break
    return best

def parse_close_ts(ct: str) -> int | None:
    for fmt in ("%Y-%m-%d %H:%M:%S%z",):
        try:
            return int(datetime.strptime(ct.replace("+00", "+0000"), fmt).timestamp())
        except Exception:
            pass
    try:
        return int(datetime.strptime(ct, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None

def fetch_history(market: dict):
    yes_tok = market["_yes_tok"]
    hist_raw = curl(f"{CLOB}?market={yes_tok}&interval=max&fidelity=60")
    if not hist_raw: return None
    try:
        hist = json.loads(hist_raw).get("history", [])
    except Exception:
        return None
    if len(hist) < 5: return None
    prices = {round(pt["p"], 3) for pt in hist if pt.get("p") is not None}
    if len(prices) < 2: return None
    close_ts = market["_close_ts"]
    p1h = price_at_lead(hist, close_ts, 3600)
    p24h = price_at_lead(hist, close_ts, 86400)
    if p1h is None and p24h is None: return None
    return {
        "cid": market["conditionId"], "q": market.get("question", "")[:120],
        "cat": categorize(market.get("question", ""), market.get("slug", "")),
        "vol": float(market.get("volume") or 0), "close_ts": close_ts,
        "realized": market["_realized"], "p1h": p1h, "p24h": p24h,
    }

def main():
    vol_min = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    records = []
    seen = set()
    for offset in range(0, 2401, 100):
        url = (f"{GAMMA}?closed=true&limit=100&offset={offset}&order=closedTime"
               f"&ascending=false&volume_num_min={vol_min}")
        raw = curl(url)
        if not raw:
            print(f"offset {offset}: FETCH FAIL", file=sys.stderr); continue
        try:
            markets = json.loads(raw)
        except Exception:
            print(f"offset {offset}: JSON FAIL", file=sys.stderr); continue
        if not isinstance(markets, list) or not markets:
            print(f"offset {offset}: end ({str(markets)[:60]})", file=sys.stderr); break
        candidates = []
        for m in markets:
            if not isinstance(m, dict): continue
            cid = m.get("conditionId")
            if not cid or cid in seen: continue
            realized = clean_binary(m)
            if realized is None: continue
            ct = m.get("closedTime")
            if not ct: continue
            close_ts = parse_close_ts(ct)
            if close_ts is None: continue
            try:
                toks = json.loads(m["clobTokenIds"])
            except Exception:
                continue
            if not toks: continue
            seen.add(cid)
            m["_yes_tok"] = toks[0]; m["_close_ts"] = close_ts; m["_realized"] = realized
            candidates.append(m)
        kept = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            for rec in ex.map(fetch_history, candidates):
                if rec: records.append(rec); kept += 1
        print(f"offset {offset}: got={len(markets)} cand={len(candidates)} kept={kept} total={len(records)}", file=sys.stderr)
        with open("/tmp/pm_calibration_data.json", "w") as f:
            json.dump(records, f)
    print(f"SAVED {len(records)} records", file=sys.stderr)

if __name__ == "__main__":
    main()
