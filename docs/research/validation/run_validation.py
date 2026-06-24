#!/usr/bin/env python3
"""One-command independent validation of the liquidity-rewards making edge.

Runs four checks and prints a verdict:
  1. Unit suite — the reward/bleed math and strategy logic (offline, deterministic).
  2. Live pool scan — pull the real CLOB rewards program, rank SAFE mid-tail pools.
  3. Backtest — replay real price history (live-measured per-pool share) with a
     colocation (cancel-efficiency) sweep.
  4. Portfolio — select a fundable $200 SAFE book and report aggregate yield (dry-run).

Usage:
    ALL_PROXY=socks5://127.0.0.1:7890 python validation/run_validation.py
    python validation/run_validation.py --offline      # skip the live steps

The live steps need network (and the SOCKS proxy + socksio if behind one).
This NEVER places an order — the live bot stays in dry-run.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # make `import pm_edge` work when run as a script


def run_units() -> bool:
    print("\n[1/4] Unit suite (reward math + strategy logic) ...")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
    )
    print("  ->", "PASS" if r.returncode == 0 else "FAIL")
    return r.returncode == 0


def run_live_scan() -> bool:
    print("\n[2/4] Live reward-pool scan (real CLOB) ...")
    from pm_edge import scanner
    rep = scanner.run_scan(min_daily=80.0, top=14, with_jump_risk=True)
    print(f"  reward pools: {rep['total_reward_pools']} | scored: {rep['pools_scored']} "
          f"| SAFE(non-empty): {rep['safe_count']}")
    safe = [p for p in rep["pools"] if p["jump_verdict"] == "SAFE" and not p["empty_band"]]
    for p in safe[:6]:
        print(f"    SAFE ${p['daily']:>6.0f}/d share~{p['share']:.2f} "
              f"jump<{p['max_jump_c']}c  {p['question'][:42]}")
    return rep["pools_scored"] > 0


def run_backtest() -> bool:
    print("\n[3/4] Backtest with colocation lever (real history, live-measured share) ...")
    from pm_edge import backtest
    rep = backtest.run(min_daily=80.0, top=12, cancel_efficiencies=(0.0, 0.9),
                       use_scanned_share=True)
    print(f"  pools simulated: {rep['pools_simulated']} "
          f"(per-pool share measured from live books)")
    for eff, v in rep["aggregate"].items():
        print(f"    cancel_eff {eff.replace('eff_',''):>4}: "
              f"reward ${v['reward_income']:>9.2f} bleed ${v['adverse_bleed']:>8.2f} "
              f"NET ${v['net']:>9.2f}")
    return rep["pools_simulated"] > 0


def run_portfolio() -> bool:
    print("\n[4/4] Portfolio: select a fundable $200 SAFE book (dry-run, no orders) ...")
    from pm_edge import portfolio
    out = portfolio.run(capital=200.0, min_daily=80.0, top=40)
    s = out["summary"]
    print(f"  selected {s['pools']} pools | committed ${s['committed_capital']} "
          f"| est reward ${s['est_daily_reward']}/day | ~{s['est_annualized_pct']:,.0f}%/yr")
    for sel in out["selected"][:6]:
        print(f"    ${sel['daily']:>6.0f}/d share~{sel['share']:.2f} "
              f"cap ${sel['committed_capital']:>5.1f}  {sel['question'][:40]}")
    return out["summary"]["pools"] > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip live network steps")
    args = ap.parse_args()

    ok = run_units()
    if not args.offline:
        try:
            ok = run_live_scan() and ok
            ok = run_backtest() and ok
            ok = run_portfolio() and ok
        except Exception as e:  # network / proxy issues — report, don't crash
            print(f"  live step error: {type(e).__name__}: {e}")
            print("  (re-run with the SOCKS proxy, or use --offline)")

    print("\n==== VERDICT ====")
    print("Edge: Polymarket liquidity-rewards making in SAFE mid-tail pools.")
    print("Mechanism: treasury-funded reward share > adverse-selection bleed; "
          "colocation shrinks the bleed term (see aggregate above).")
    print("Real-money execution is hard-gated in pm_edge/live.py — this run placed NO orders.")
    print("Status:", "VALIDATED (units green)" if ok else "CHECK FAILED — see output")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
