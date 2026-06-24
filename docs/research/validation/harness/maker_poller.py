#!/usr/bin/env python3
"""Periodic maker-rewards accrual poller for the laoda paper account.
Runs the reward-aware paper strategy live: every INTERVAL it polls each active
maker quote's real book+mid, accrues reward, charges adverse bleed, and logs the
running net P&L. This is the virtual-pond experiment actually running over time.
"""
import json, time
from pathlib import Path
from pm_trader.engine import Engine

INTERVAL = 120
ROUNDS = 40   # ~80 minutes
log = open("/tmp/maker_run.jsonl", "a")
eng = Engine(Path.home() / ".pm-trader" / "laoda")
try:
    for rnd in range(ROUNDS):
        try:
            acc = eng.accrue_maker_rewards()
            summ = eng.get_maker_summary()
        except Exception as e:
            log.write(json.dumps({"rnd": rnd, "error": type(e).__name__, "msg": str(e)[:80]}) + "\n")
            log.flush(); time.sleep(INTERVAL); continue
        rec = {
            "rnd": rnd, "t": int(time.time()),
            "active": summ["active_quotes"],
            "committed": round(summ["committed_capital"], 2),
            "reward_income": round(summ["reward_income"], 6),
            "adverse_bleed": round(summ["adverse_bleed"], 6),
            "net_maker_pnl": round(summ["net_maker_pnl"], 6),
            "per_quote": [{"id": r["quote"]["id"], "reward": r["reward"],
                           "bleed": r["bleed"], "share": r["share"], "mid": r["mid"]}
                          for r in acc],
        }
        log.write(json.dumps(rec) + "\n"); log.flush()
        time.sleep(INTERVAL)
finally:
    eng.close(); log.close()
print("poller done", ROUNDS, "rounds")
