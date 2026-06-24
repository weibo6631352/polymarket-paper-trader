"""Periodic market discovery — re-scan the FULL reward-pool universe on a schedule.

Reward pools churn constantly (new ones funded, others resolve, jump-risk shifts),
so the SAFE candidate set must be refreshed, not scanned once.  ``refresh`` runs one
full (paginated, uncapped) scan and writes the current SAFE pools to a file; ``watch``
repeats it every ``interval_s``.  Read-only — discovery never places orders.
"""

from __future__ import annotations

import json
import time

from pm_trader.rewards import RewardsClient, scan


def refresh(client: RewardsClient, *, out_path: str | None = None, **scan_kwargs) -> dict:
    """One full scan → a SAFE-pool summary, optionally persisted to ``out_path``."""
    report = scan(client, **scan_kwargs)
    safe = [p for p in report["pools"]
            if p["jump_verdict"] == "SAFE" and not p["empty_band"]]
    summary = {
        "total_reward_pools": report["total_reward_pools"],
        "pools_scored": report["pools_scored"],
        "safe_count": len(safe),
        "safe": safe,
    }
    if out_path is not None:
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2)
    return summary


def watch(
    client: RewardsClient,
    *,
    interval_s: float,
    rounds: int,
    out_path: str | None = None,
    sleeper=time.sleep,
    **scan_kwargs,
) -> list[dict]:
    """Re-run ``refresh`` ``rounds`` times, ``interval_s`` apart (sleeper injectable)."""
    results: list[dict] = []
    for i in range(max(0, rounds)):
        results.append(refresh(client, out_path=out_path, **scan_kwargs))
        if i < rounds - 1:
            sleeper(interval_s)
    return results


def run(*, watch_rounds: int = 1, interval_s: float = 1800.0,
        out_path: str | None = None, **scan_kwargs) -> list[dict]:
    """Convenience wrapper: build a client, watch for ``watch_rounds``, close it."""
    client = RewardsClient()
    try:
        return watch(client, interval_s=interval_s, rounds=watch_rounds,
                     out_path=out_path, **scan_kwargs)
    finally:
        client.close()
