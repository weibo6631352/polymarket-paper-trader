# 01 — Edge Discovery: the multi-dimension hunt

**Question.** Where, if anywhere, is there a durable, real, capturable edge on Polymarket — across structure, math, statistics, microstructure, and behaviour?

**Method.** A multi-agent hunt generated **39 candidate edges** across six dimensions, then adversarially refuted and quantified each. Only **4 survived**, and only **1** is a real money-maker; the rest are defensive filters or selection aids.

## The six dimensions and their verdicts

| Dimension | Verdict | Why |
|---|---|---|
| **Liquidity-rewards market-making** | ✅ **The durable edge** | Polymarket pays a fixed daily USDC pool from its **own treasury** to resting in-band quotes. Non-zero-sum subsidy → not arbitraged to zero. |
| Structural arbitrage (neg-risk fields, cross-platform) | ❌ Dead | The WC-winner field (60 outcomes) sums to 1.009 mid; buying the whole field costs **$1.037** for $1 — a 3.7% loss. Apparent `sum < 1` is unquoted placeholder legs, not free money. |
| Statistical / calibration bias | ❌ No blanket bias | 1,587 resolved markets: PM is well-calibrated; no harvestable favorite-longshot bias (see [03](03-calibration-study.md)). |
| Sports-sharp directional (soccer derivatives) | ❌ No standing edge | A Dixon-Coles model anchored to PM's own totals+moneyline cleared **0** derivative legs by ≥5c across 24 WC matches. The complex is internally coherent. |
| Copy-trading by name | ❌ Useless | Top realized-PnL leaders are flat; the few still holding have already-moved or near-resolved positions. |
| Bot strategy by style | ↳ Informative | Reverse-engineering 28 wallets (see [02](02-bot-strategy-atlas.md)) showed the biggest real edge **is** market-making — but that maps straight back to the rewards edge. |

## Why liquidity-rewards making is the one that survives

A mispricing edge gets competed to zero because someone on the other side is losing money and learns. The reward pool is **different**: Polymarket funds it from its treasury to bootstrap liquidity. No counterparty is being "beaten." So it decays only toward the equilibrium

```
reward_share  ≈  adverse_selection_bleed  +  capital_cost
```

The **one lever** that shifts that equilibrium in your favour is **latency**: sub-millisecond cancels turn would-be toxic fills into avoided fills, shrinking the bleed term. That is precisely why colocating near Polymarket's matching infra is the right scaling play — and why the backtest in this repo carries an explicit `cancel_efficiency` parameter.

## What "the edge" actually is

Rest two-sided quotes one tick inside the reward band of **mid-tail pools** ($100–500/day, thin in-band competition), earn a share of the daily pool, manage inventory, and cancel fast on jumps. See [04](04-lp-rewards-edge.md) for the full mechanism, the live confirmation, the binding constraint (capacity, not per-trade EV), and the experiment results.

> The verified implementation of this edge lives in [`../pm_edge`](../pm_edge); reproduce the validation with [`../validation/run_validation.py`](../validation/run_validation.py).
