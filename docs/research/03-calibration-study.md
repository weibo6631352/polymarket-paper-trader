# 03 — Calibration Study: is there a favorite-longshot bias to harvest?

**Question.** When Polymarket prices an outcome at probability *p*, does it occur with frequency *p*? A systematic gap (longshots over-priced, favorites under-priced, monotonic) would be a free statistical edge.

**Data.** 1,587 resolved binary markets, volume ≥ $10k, clean 1/0 resolution. Forecast = YES-token CLOB price at a fixed lead time before close; outcome = final resolution. Brier 0.112. (Raw data: [`../data/pm_calibration_data.json`](../data/pm_calibration_data.json); collector + analysis in [`../validation/harness/`](../validation/harness/).)

## Verdict: PM is well-calibrated — no clean harvestable bias

| Region | n | priced | realized | gap |
|---|---|---|---|---|
| Longshots ≤ 0.10 | 602 | 1.21% | 1.83% | +0.6c (sign is *reverse* of classic LSB; too small vs ~2c spread) |
| Extreme favorites ≥ 0.90 | 215 | 99.09% | 99.07% | −0.02c (dead-on) |
| Mid 0.80–0.90 (all) | 40 | 84.9% | 72.5% | **−12c** — the one notable effect |

- **Longshots and extreme favorites are fair to within sub-cent accuracy.** No free lunch there.
- The **only** signal is a mild over-pricing of **0.80–0.90 favorites, concentrated in sports** (sports subset n=35: 0.849 → 0.714). Direction = *fade* high-priced sports favorites / buy their NO — the **opposite** of "back the favorite." But it's borderline-significant (z ≈ −2), thin-sample, and category-specific.
- The effect is **not** larger 24h out (prices are simply less sharp, not more biased), and does not concentrate in any liquidity tier.

## Caveats
- Selection bias: only resolved, liquid, traded markets — not a random draw.
- Political markets are under-sampled (API offset cap blocks the largest historical ones), so the cleanest "stale-news lag" category is untested here.
- The 0–0.02 and 0.98–1.0 bins (~42% of the sample) are near-settled and trivially calibrated; the real action is the 0.10–0.90 mid-range.

## So what
There is **no blanket price-bucket edge**. This is a *negative* result that matters: it rules out the most-attempted retail "strategy" and redirects effort to the structural reward edge, which does not depend on out-predicting the market at all. It also reframes the live "favorite harvester" bots (see [02](02-bot-strategy-atlas.md)): their steady green is largely *unrealized* mark-to-market drift, not a proven settlement edge.
