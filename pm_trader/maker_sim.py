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

from pm_trader.models import OrderBook, OrderBookLevel
from pm_trader.orderbook import (
    adverse_bleed,
    book_inband_qmin,
    maker_reward_share,
    reward_accrual,
)
from pm_trader.rewards import RewardsClient, parse_rewards, MIN_DAILY

SECONDS_PER_DAY = 86_400.0


def live_pool_share(client: RewardsClient, pool: dict) -> float | None:
    """Estimate a pool's CURRENT reward share for a min_size one-tick quote.

    Pulls the live book, builds the binding-side competing Qmin, and returns the
    realistic share (vs a flat assumption).  Returns ``None`` if the book is
    empty/one-sided or the fetch fails.
    """
    try:
        raw = client.book(pool["token"])
    except Exception:
        return None
    bids = [OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in (raw.get("bids") or []) if "price" in b and "size" in b]
    asks = [OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in (raw.get("asks") or []) if "price" in a and "size" in a]
    if not bids or not asks:
        return None
    mid = (max(b.price for b in bids) + min(a.price for a in asks)) / 2.0
    qmin = book_inband_qmin(OrderBook(bids=bids, asks=asks), mid, pool["max_spread"])
    return maker_reward_share(pool["min_size"], pool["tick"] * 100.0, pool["max_spread"], qmin)


def simulate_pool(
    price_path: list[float],
    *,
    daily: float,
    share: float,
    tick: float,
    min_size: float,
    dt_seconds: float,
    cancel_efficiency: float = 0.0,
    requote_downtime_s: float = 0.0,
    unwind_cost_ticks: float = 0.0,
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
        requote_downtime_s: seconds of zero reward after each pickoff while the
            maker is one-sided (holding inventory) before it re-hedges and
            restores its two-sided Qmin.  0 = no downtime (continuous quoting).
            Faster automation/colocation lowers this too.

        unwind_cost_ticks: ticks of spread paid to flatten the one-sided inventory
            left by each pickoff (you cross the book back to get flat).  0 = ignore;
            ~1-2 ticks is realistic.  Scales with the fill probability like bleed.

    Returns a dict with reward_income, adverse_bleed, unwind_cost, net, pickoffs,
    steps, capital (≈ min_size), and net_annualized_pct.
    """
    if not 0.0 <= cancel_efficiency <= 1.0:
        raise ValueError("cancel_efficiency must be in [0, 1]")
    if requote_downtime_s < 0:
        raise ValueError("requote_downtime_s must be >= 0")
    if unwind_cost_ticks < 0:
        raise ValueError("unwind_cost_ticks must be >= 0")
    n_steps = max(0, len(price_path) - 1)
    # Reuse the engine's canonical pure functions so the backtest and the live
    # paper engine never diverge.  Quote sits half_spread = one tick from mid.
    half_spread_c = tick * 100.0
    fill_frac = 1.0 - cancel_efficiency           # P(actually filled) — colocation cancels
    unwind_per_fill = min_size * unwind_cost_ticks * tick
    bleed_total = 0.0
    unwind_total = 0.0
    pickoffs = 0
    for i in range(1, len(price_path)):
        raw = adverse_bleed(min_size, half_spread_c, price_path[i - 1], price_path[i])
        if raw > 0:                # mid crossed the quote → stale side picked off
            pickoffs += 1
            bleed_total += raw * fill_frac                 # mark loss of the stale fill
            unwind_total += unwind_per_fill * fill_frac    # cost to flatten the inventory
    # Reward only accrues while two-sided in-band; each pickoff costs downtime
    # (you are one-sided holding inventory until you re-hedge).
    gross_seconds = dt_seconds * n_steps
    downtime = min(gross_seconds, pickoffs * requote_downtime_s)
    reward_income = reward_accrual(share, daily, gross_seconds - downtime)
    net = reward_income - bleed_total - unwind_total
    capital = max(min_size, 1e-9)  # two-sided min_size locks ≈ min_size dollars
    horizon_seconds = max(gross_seconds, 1e-9)
    net_per_day = net * SECONDS_PER_DAY / horizon_seconds
    net_ann = net_per_day * 365 / capital * 100
    return {
        "steps": n_steps,
        "reward_income": round(reward_income, 4),
        "adverse_bleed": round(bleed_total, 4),
        "unwind_cost": round(unwind_total, 4),
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
    use_scanned_share: bool = True,
    fidelity: int = 1440,
    requote_downtime_s: float = 0.0,
    unwind_cost_ticks: float = 0.0,
) -> dict:
    """Backtest the maker strategy on real CLOB price history for top pools.

    For each reward pool (daily ≥ ``min_daily``), pull its price history at
    ``fidelity`` minutes (finer = more honest intraday pickoff) and simulate a
    two-sided ``min_size`` maker at each ``cancel_efficiencies`` level, so the
    colocation lever's effect is explicit.  With ``use_scanned_share`` the per-pool
    reward share is measured from the live book (vs a flat ``share`` assumption),
    so the reward side is realistic rather than a uniform guess.  Returns per-pool
    results plus an aggregate ``net`` per efficiency level.
    """
    markets = client.sampling_markets()
    pools = [p for p in (parse_rewards(m) for m in markets) if p and p["daily"] >= min_daily]
    pools.sort(key=lambda p: -p["daily"])
    pools = pools[: max(1, top)]

    per_pool: list[dict] = []
    agg = {eff: {"reward": 0.0, "bleed": 0.0, "unwind": 0.0, "net": 0.0}
           for eff in cancel_efficiencies}
    for p in pools:
        try:
            history = client.prices_history(p["token"], fidelity=fidelity)
        except Exception:
            continue
        path = _path_from_history(history)
        if len(path) < 10:
            continue
        pool_share = share
        if use_scanned_share:
            measured = live_pool_share(client, p)
            if measured is not None:
                pool_share = measured
        dt = _history_dt_seconds(history)
        sims = {}
        for eff in cancel_efficiencies:
            r = simulate_pool(
                path, daily=p["daily"], share=pool_share, tick=p["tick"],
                min_size=p["min_size"], dt_seconds=dt, cancel_efficiency=eff,
                requote_downtime_s=requote_downtime_s,
                unwind_cost_ticks=unwind_cost_ticks,
            )
            sims[f"eff_{eff}"] = r
            agg[eff]["reward"] += r["reward_income"]
            agg[eff]["bleed"] += r["adverse_bleed"]
            agg[eff]["unwind"] += r["unwind_cost"]
            agg[eff]["net"] += r["net"]
        per_pool.append({
            "question": p["question"],
            "daily": round(p["daily"], 2),
            "min_size": p["min_size"],
            "share_used": round(pool_share, 4),
            "dt_seconds": dt,
            "path_points": len(path),
            "sims": sims,
        })

    aggregate = {
        f"eff_{eff}": {
            "reward_income": round(v["reward"], 2),
            "adverse_bleed": round(v["bleed"], 2),
            "unwind_cost": round(v["unwind"], 2),
            "net": round(v["net"], 2),
        }
        for eff, v in agg.items()
    }
    return {
        "params": {"min_daily": min_daily, "top": top, "share": share,
                   "cancel_efficiencies": list(cancel_efficiencies),
                   "use_scanned_share": use_scanned_share, "fidelity": fidelity,
                   "requote_downtime_s": requote_downtime_s,
                   "unwind_cost_ticks": unwind_cost_ticks},
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
