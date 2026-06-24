#!/usr/bin/env python3
"""Calibration analysis of Polymarket resolved binary markets.

Reads /tmp/pm_calibration_data.json (list of records with p1h, p24h, realized, cat, vol).
Bins forecast prices and compares to realized win rates. The gap (realized - forecast)
is the exploitable miscalibration.
"""
from __future__ import annotations
import json, math, statistics

BINS = [0.0, 0.02, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
        0.60, 0.70, 0.80, 0.90, 0.95, 0.98, 1.0001]

def bin_label(b: list, i: int) -> str:
    lo, hi = b[i], b[i+1]
    return f"{lo:.2f}-{min(hi,1.0):.2f}"

def wilson_ci(k: int, n: int, z: float = 1.96):
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    half = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / denom
    return (center - half, center + half)

def calibrate(pairs: list[tuple[float, int]]):
    """pairs = [(forecast, realized 0/1)]. Returns per-bin stats."""
    buckets = [[] for _ in range(len(BINS)-1)]
    for f, r in pairs:
        for i in range(len(BINS)-1):
            if BINS[i] <= f < BINS[i+1]:
                buckets[i].append((f, r)); break
    rows = []
    for i, bk in enumerate(buckets):
        n = len(bk)
        if n == 0:
            rows.append((bin_label(BINS, i), 0, None, None, None, None, None)); continue
        mean_f = sum(f for f, _ in bk) / n
        wins = sum(r for _, r in bk)
        rate = wins / n
        gap = rate - mean_f
        lo, hi = wilson_ci(wins, n)
        rows.append((bin_label(BINS, i), n, mean_f, rate, gap, lo, hi))
    return rows

def brier(pairs):
    if not pairs: return None
    return sum((f - r)**2 for f, r in pairs) / len(pairs)

def print_table(title: str, pairs: list):
    rows = calibrate(pairs)
    print(f"\n=== {title}  (N={len(pairs)}, Brier={brier(pairs):.4f}) ===")
    print(f"{'bin':>11} | {'n':>5} | {'mean_fcst':>9} | {'realized':>8} | {'gap':>7} | {'95% CI realized':>18}")
    print("-"*78)
    for lbl, n, mf, rate, gap, lo, hi in rows:
        if n == 0:
            print(f"{lbl:>11} | {n:>5} |     -     |    -     |    -    |")
            continue
        ci = f"[{lo:.3f},{hi:.3f}]"
        sig = ""
        # flag bins where forecast lies OUTSIDE the realized CI (significant miscalibration)
        if mf < lo or mf > hi:
            sig = "  <-- forecast outside CI"
        print(f"{lbl:>11} | {n:>5} | {mf:>9.4f} | {rate:>8.4f} | {gap:>+7.4f} | {ci:>18}{sig}")

def main():
    data = json.load(open("/tmp/pm_calibration_data.json"))
    print(f"Loaded {len(data)} records")
    cats = {}
    for d in data: cats[d["cat"]] = cats.get(d["cat"], 0) + 1
    print("Category counts:", cats)

    # ---- 1h lead time ----
    pairs_1h = [(d["p1h"], d["realized"]) for d in data if d.get("p1h") is not None]
    print_table("1h before close — ALL", pairs_1h)

    # ---- 24h lead time ----
    pairs_24h = [(d["p24h"], d["realized"]) for d in data if d.get("p24h") is not None]
    print_table("24h before close — ALL", pairs_24h)

    # ---- by category (1h) ----
    for cat in ["sports", "crypto", "politics", "other"]:
        cp = [(d["p1h"], d["realized"]) for d in data if d.get("p1h") is not None and d["cat"] == cat]
        if len(cp) >= 30:
            print_table(f"1h — category={cat}", cp)

    # ---- by liquidity tier (1h) ----
    vols = sorted(d["vol"] for d in data)
    if vols:
        med = vols[len(vols)//2]
        lo_t = [(d["p1h"], d["realized"]) for d in data if d.get("p1h") is not None and d["vol"] < med]
        hi_t = [(d["p1h"], d["realized"]) for d in data if d.get("p1h") is not None and d["vol"] >= med]
        print(f"\n(volume median = {med:.0f})")
        print_table(f"1h — LOW volume (<{med:.0f})", lo_t)
        print_table(f"1h — HIGH volume (>={med:.0f})", hi_t)

    # ---- favorite/longshot summary (1h) ----
    print("\n=== FAVORITE / LONGSHOT SUMMARY (1h) ===")
    longshots = [(f, r) for f, r in pairs_1h if f <= 0.10]
    favs = [(f, r) for f, r in pairs_1h if f >= 0.90]
    if longshots:
        mf = sum(f for f,_ in longshots)/len(longshots)
        rr = sum(r for _,r in longshots)/len(longshots)
        print(f"Longshots (fcst<=0.10): n={len(longshots)} mean_fcst={mf:.4f} realized={rr:.4f} gap={rr-mf:+.4f}")
    if favs:
        mf = sum(f for f,_ in favs)/len(favs)
        rr = sum(r for _,r in favs)/len(favs)
        print(f"Favorites (fcst>=0.90): n={len(favs)} mean_fcst={mf:.4f} realized={rr:.4f} gap={rr-mf:+.4f}")

if __name__ == "__main__":
    main()
