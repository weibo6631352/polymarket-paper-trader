# Findings — profitability investigation (2026-06-16)

Read-only/paper audit of whether this simulator can be traded profitably. No real
orders, no funds. The simulator itself is faithful and credible — it does **not**
hand out fake edges, which is the whole point below.

## Strategies tested on live order books (paper)

| Strategy | Result | Why |
|---|---|---|
| Momentum (taker) | **−33%**, 44 trades, 0% win | pays spread every round-trip (~−30% median round-trip drag) + buys into mean reversion |
| EWMA mean-reversion (taker) | flat, 0 trades | signal never triggered on tight-spread liquid markets |
| Passive market-making (limit) | flat, **0 fills** | sim has no phantom counterparty flow — resting quotes only fill on real adverse moves |
| Tick-scalper (limit) | flat, **0 fills** | same — books don't move to the bid in short windows |

No risky strategy produced a credible positive in-session curve or Sharpe.

## The only real edge: neg-risk settlement arbitrage

Polymarket "neg-risk" events are mutually-exclusive **and** exhaustive — exactly one
candidate's YES resolves to \$1. Buy 1 share of YES on **every** candidate for a total
< \$1 → guaranteed \$1 at resolution. Real, risk-free, and the sim reproduces it
correctly (verified + executed against live CLOB books).

**Measured live capacity (2026-06-16, 500 events / 168 neg-risk):**
- Only 3 fully-buyable events with real depth.
- **Total deployable ≈ \$1,360 → ≈ \$16.5 guaranteed profit (+1.2%).**
- Capital locked until resolution, capital-weighted **~205 days** → **≈ 2.2% annualized**,
  which is **below the risk-free rate**. After opportunity cost it doesn't beat cash.

## Bottleneck = capacity, not latency

The binding constraint is **size**: the edge lives only at top-of-book and decays to
zero within tens of shares (e.g. Maduro event 4.8% → gone by 76 units). Total live
capacity is ~\$1.4k — too small to matter ("不解渴").

**Secondary speed note (for a possible future low-latency / Dublin deployment):** the
*only* latency-relevant angle is the race to grab a fleeting arb gap before competitors
or the neg-risk mint/convert mechanism close it. Worth recording as an observation, but
it does **not** change the main conclusion — even won perfectly, the prize is ~\$16.

## Verdict

Keep this repo as a credible paper-trading simulator and agent tool — it's good at
that. It is **not** a money-maker: the one genuine edge is real but ~\$1.4k / ~2.2%
annualized, capacity-bound and below cash.
