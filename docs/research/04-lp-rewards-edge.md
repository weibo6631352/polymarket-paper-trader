# 04 — The LP-Rewards Edge (the verified, profitable one)

## Mechanism
Polymarket runs a **liquidity-rewards program**: it pays a fixed **daily USDC pool** from its own treasury to limit orders resting within `max_spread` of the midpoint, two-sided, size-weighted by `((c − s) / c)²` where `s` is the order's distance from mid (cents) and `c = max_spread`. The pool is split by the binding (lighter) two-sided **Qmin** score. Your daily income:

```
reward_per_day      = your_share × pool_daily_rate
your_share          ≈ your_Qmin / (your_Qmin + competing_Qmin)
adverse_bleed/event ≈ size × (|Δmid| − half_spread)     # when mid jumps your offset
net                 = reward_income − adverse_bleed
capital_locked      ≈ size × (1 − 2·half_spread)         # two-sided min_size quote
```

All of this is the pure math in [`../pm_edge/reward_math.py`](../pm_edge/reward_math.py).

## Why it's durable (not arbitraged to zero)
It is a **subsidy**, not a bet against a sharper counterparty. No one on the other side is losing and learning. It decays only toward `reward_share ≈ bleed + capital_cost`. The one in-scope lever is **latency** — faster cancels shrink the bleed term. This is the colocation thesis, and the backtest models it as `cancel_efficiency`.

## Live confirmation (real CLOB `/sampling-markets`)
- ~1,000 reward markets, **all** with `daily_rate > 0`; ~$24k/day total subsidy.
- Marquee pools (France WC $3,488/day) have millions of in-band depth → your share ≈ 0 → **KILL** (one jump wipes 56–132 days of reward).
- The business is the **mid-tail**: ~$300–500/day political-nominee pools (NYC council Dem noms, Maryland Gov) and $100/day "Trump meet with X" pools, `min_size` 50, `max_spread` 4.5c — thin in-band competition, so a single `min_size` quote captures a meaningful share.

## The binding constraint: capacity, not per-trade EV
Two-sided `min_size` locks ~$50–100 and earns ~$2/day/pool — **huge %, tiny $**. Scaling needs many pools + continuous two-sided quoting + inventory management (a one-sided fill kills the Qmin reward) + fast cancels. **Key inversion:** at a $200–$2k scale this edge is *not* capacity-constrained — it is the *right-sized* edge for a small account, contrary to the "uncopyable MM" framing (which is about matching million-dollar makers).

The other tension is structural: **low realized vol ⟺ deep/liquid ⟺ low share**, while **high share ⟺ thin in-band ⟺ high jump risk**. The edge lives in the narrow band between, which the [scanner](../pm_edge/scanner.py) ranks (gross yield) and jump-filters (SAFE/WATCH/KILL).

## Experiments (all paper / dry-run — no real money)
1. **Backtest, colocation sweep** (real CLOB history): adverse bleed scales linearly with `(1 − cancel_efficiency)` — e.g. $2,390 → $239 from eff 0.0 → 0.9 over the horizon. Quantifies the colocation lever directly. (Daily-fidelity history smooths intraday pickoff, so absolute net is optimistic; the *relative* colocation effect is the clean signal.)
2. **Live paper portfolio (81 min, $200 book):** 3 diversified SAFE quotes (Dan Cox / Beth Davidson / Ed Hale), $148 committed. Result **net +$6.20** (reward $8.50 − bleed $2.30), including one real ~$2.25 pickoff caught live at +62 min. Net stayed positive; shares drifted 0.24 → 0.20 as competition arrived. Run-rate ~$110/day on $148 ≈ 27,000%/yr **gross in a benign window** — *not* a sustainable annualized figure; sustained net requires surviving discrete resolution jumps over weeks.
3. **Live dry-run bot** on real pools: computed real two-sided orders (e.g. Dan Cox BUY 0.88 / SELL 0.90 ×50, est share 23.6%), and on a +2-tick move correctly planned `CANCEL_ALL` + repost. All tagged `DRY_RUN`; the live signer is hard-gated.
4. **Longer paper run (4.0 h, diversified 3-pool book, $167 committed):** isolating the fresh book's incremental P&L (the engine summary is account-cumulative, so we diff), net **+$0.88** (reward +$1.13, bleed +$0.25 from **one** pickoff) → ~$5.2/day ≈ **1,144%/yr**. Two honest lessons: (a) the reward rate was ~$0.28/h vs ~$6.3/h for the earlier high-share book — **share compresses** as you pick lower-competition pools / reveal your quote, so the 27,000%/yr short-window figure was selection-optimistic; (b) only **one** discrete jump occurred in a calm 4 h, so true jump-survival still needs a multi-day/week run. Net stayed positive throughout.

## Conservatism in the backtest
The backtest charges costs on **both** sides, not just bleed: per pickoff it deducts
the stale-fill mark loss (`adverse_bleed`), a re-hedge **downtime** (zero reward while
one-sided, `requote_downtime_s`), and an inventory **unwind cost** (ticks of spread to
flatten, `unwind_cost_ticks`). Reward share is **measured from the live book** per pool
rather than assumed. All three frictions shrink with `cancel_efficiency` (colocation),
which is exactly why latency is the lever. Defaults are zero so you can dial realism up
and watch the edge degrade gracefully.

## Honest status
Mechanically **CONFIRMED net-positive** in real time, surviving a live pickoff. The annualized number is regime-dependent and optimistic until tested across weeks of jumps. The implementation is built to one operator switch from live (see [`../pm_edge/live.py`](../pm_edge/live.py) and the README's "Going live" section) — **real-money submission is intentionally not automated.**
