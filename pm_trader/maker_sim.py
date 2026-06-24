"""Maker-rewards paper simulator — risk-free virtual trading of the
liquidity-rewards market-making edge.

The paper Engine is taker-only and cannot model reward accrual for resting
orders, so this module simulates the maker strategy directly:

  Post two-sided ``min_size`` quotes one tick inside a reward pool's band.
  Each time step the quote sits in-band it earns a share of the pool's daily
  USDC rate.  When the midpoint moves through the quote the stale side is
  picked off — an adverse fill that costs roughly ``(|Δmid| - tick) * size``.
  A colocated bot cancels faster, avoiding a fraction of that bleed; that
  fraction is the ``cancel_efficiency`` lever (0 = slow / always picked off,
  1 = perfect / never picked off).

  net = reward_income - adverse_bleed

This makes the central thesis testable: does a mid-tail pool net positive, and
how much does colocation (faster cancels) shift it?  Drive it with real CLOB
price history (``run_experiment``) for an honest backtest, or with a synthetic
path for unit tests.
"""

from __future__ import annotations

from pm_trader.orderbook import adverse_bleed, reward_accrual
from pm_trader.rewards import RewardsClient, parse_rewards, MIN_DAILY

SECONDS_PER_DAY = 86_400.0


def simulate_pool(
    price_path: list[float],
    *,
    daily: float,
    share: float,
    tick: float,
    min_size: float,
    dt_seconds: float,
    cancel_efficiency: float = 0.0,
) -> dict:
    """Simulate a two-sided min_size maker over a midpoint price path.

    Args:
        price_path: sequence of midpoints over time (one per step).
        daily: pool's daily reward rate (USD).
        share: maker's reward share of the pool (0-1) while quoting in-band.
        tick: minimum tick size (price units); quotes sit one tick from mid.
        min_size: shares quoted per side.
        dt_seconds: wall-clock seconds per step (sets reward per step).
        cancel_efficiency: fraction of adverse fills avoided by fast cancels
            (0 = always picked off, 1 = never).  The colocation lever.

    Returns a dict with reward_income, adverse_bleed, net, pickoffs, steps,
    capital (≈ min_size), and net_annualized_pct.
    """
    if not 0.0 <= cancel_efficiency <= 1.0:
        raise ValueError("cancel_efficiency must be in [0, 1]")
    n_steps = max(0, len(price_path) - 1)
    # Reuse the engine's canonical pure functions so the backtest and the live
    # paper engine never diverge.  Quote sits half_spread = one tick from mid.
    half_spread_c = tick * 100.0
    reward_income = reward_accrual(share, daily, dt_seconds * n_steps)
    bleed_total = 0.0
    pickoffs = 0
    for i in range(1, len(price_path)):
        raw = adverse_bleed(min_size, half_spread_c, price_path[i - 1], price_path[i])
        if raw > 0:                # mid crossed the quote → stale side picked off
            pickoffs += 1
            bleed_total += raw * (1.0 - cancel_efficiency)  # colocation cancels faster
    net = reward_income - bleed_total
    capital = max(min_size, 1e-9)  # two-sided min_size locks ≈ min_size dollars
    horizon_seconds = max(dt_seconds * n_steps, 1e-9)
    net_per_day = net * SECONDS_PER_DAY / horizon_seconds
    net_ann = net_per_day * 365 / capital * 100
    return {
        "steps": n_steps,
        "reward_income": round(reward_income, 4),
        "adverse_bleed": round(bleed_total, 4),
        "net": round(net, 4),
        "pickoffs": pickoffs,
        "pickoff_rate": round(pickoffs / n_steps, 4) if n_steps else 0.0,
        "capital": round(capital, 2),
        "net_annualized_pct": round(net_ann, 1),
    }


def _history_dt_seconds(history: list[dict]) -> float:
    """Median seconds between consecutive history points (fallback 3600)."""
    ts = []
    for pt in history:
        try:
            ts.append(int(pt["t"]))
        except (KeyError, TypeError, ValueError):
            continue
    deltas = [ts[i] - ts[i - 1] for i in range(1, len(ts)) if ts[i] > ts[i - 1]]
    if not deltas:
        return 3600.0
    deltas.sort()
    return float(deltas[len(deltas) // 2])


def _path_from_history(history: list[dict]) -> list[float]:
    """Extract the midpoint price path from CLOB prices-history points."""
    path = []
    for pt in history:
        try:
            path.append(float(pt["p"]))
        except (KeyError, TypeError, ValueError):
            continue
    return path


def run_experiment(
    client: RewardsClient,
    *,
    min_daily: float = MIN_DAILY,
    top: int = 20,
    share: float = 0.05,
    cancel_efficiencies: tuple[float, ...] = (0.0, 0.9),
) -> dict:
    """Backtest the maker strategy on real CLOB price history for top pools.

    For each reward pool (daily ≥ ``min_daily``), pull its price history and
    simulate a two-sided ``min_size`` maker at each ``cancel_efficiencies``
    level, so the colocation lever's effect is explicit.  Returns per-pool
    results plus an aggregate ``net`` per efficiency level.
    """
    markets = client.sampling_markets()
    pools = [p for p in (parse_rewards(m) for m in markets) if p and p["daily"] >= min_daily]
    pools.sort(key=lambda p: -p["daily"])
    pools = pools[: max(1, top)]

    per_pool: list[dict] = []
    agg = {eff: {"reward": 0.0, "bleed": 0.0, "net": 0.0} for eff in cancel_efficiencies}
    for p in pools:
        try:
            history = client.prices_history(p["token"])
        except Exception:
            continue
        path = _path_from_history(history)
        if len(path) < 10:
            continue
        dt = _history_dt_seconds(history)
        sims = {}
        for eff in cancel_efficiencies:
            r = simulate_pool(
                path, daily=p["daily"], share=share, tick=p["tick"],
                min_size=p["min_size"], dt_seconds=dt, cancel_efficiency=eff,
            )
            sims[f"eff_{eff}"] = r
            agg[eff]["reward"] += r["reward_income"]
            agg[eff]["bleed"] += r["adverse_bleed"]
            agg[eff]["net"] += r["net"]
        per_pool.append({
            "question": p["question"],
            "daily": round(p["daily"], 2),
            "min_size": p["min_size"],
            "dt_seconds": dt,
            "path_points": len(path),
            "sims": sims,
        })

    aggregate = {
        f"eff_{eff}": {
            "reward_income": round(v["reward"], 2),
            "adverse_bleed": round(v["bleed"], 2),
            "net": round(v["net"], 2),
        }
        for eff, v in agg.items()
    }
    return {
        "params": {"min_daily": min_daily, "top": top, "share": share,
                   "cancel_efficiencies": list(cancel_efficiencies)},
        "pools_simulated": len(per_pool),
        "aggregate": aggregate,
        "pools": per_pool,
    }


def run(**kwargs: object) -> dict:
    """Convenience wrapper: build a RewardsClient, run the experiment, close it."""
    client = RewardsClient()
    try:
        return run_experiment(client, **kwargs)  # type: ignore[arg-type]
    finally:
        client.close()
