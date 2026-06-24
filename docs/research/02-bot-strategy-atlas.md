# 02 — Bot Strategy Atlas: reverse-engineering 28 profitable wallets

**Method.** From Polymarket's public leaderboards (`lb-api`) cross-referenced across 1d/7d/30d/all windows, selected the wallets that are **both** cross-window-consistent (skill, not variance) **and** currently holding (value > 0) — ~28 wallets, $138k–$3.5M each. Pulled each wallet's positions + trade activity (`data-api`) and classified the strategy.

## 28 wallets → 4 mechanisms; only 2 are copyable

### ✅ Copyable methods
1. **Sports-sharp directional taking** (~9 wallets, e.g. NiNo999, QiuYao789, GoalLineGhost). De-vig a sharp source, take the divergent side, hold to resolution. *Tested and found to have no standing model-detectable edge right now* (see [01](01-edge-discovery.md)); the edge, if any, is fleeting stale-lineup/in-play speed — not reliably capturable by a small manual account.
2. **Favorite-NO / "Nothing Ever Happens" base-rate harvest** (denizz, ~$1.6M). Buy NO at 0.70–0.90 where the unconditional base rate is genuinely lower. *Catch:* denizz's 85% win rate is really **one correlated** short-vol macro bet (a single regime shift hits a dozen NO legs at once). A small account can harvest it **only if diversified** across uncorrelated markets.

### ❌ Traps — do NOT copy
3. **HFT two-sided market-making + LP rebates** (swisstony: 0.768% margin on **$1.1B** volume; RN1, GamblingIsAllYouNeed, BrightStars). The money is spread capture + reward rebates at six-figure daily turnover. Mirroring their *positions* is actively harmful (you pay spread the other way). **But the mechanism is exactly the liquidity-rewards edge** — see [04](04-lp-rewards-edge.md). What's "uncopyable" is matching their *scale*, not the method.
4. **Longshot / futures directional** (gud.hl, suntori — actually the **same** Argentina-WC position seen through API wallet-leakage). Headline gains are unrealized mark-to-market on tickets that mostly resolve to zero; the same operators carry ~$2.4M of realized longshot wipeouts. Survivorship illusion.

## Critical data caveats (reusable)
- `data-api cashPnl` is **corrupted** for two-sided / resolved books (shows fake –$2M to –$11M) — use `/value` and `realizedPnl`.
- The `activity` endpoint caps at ~500 rows / hours-to-days → most headline PnL sits **outside** the visible window and is unverified.
- Several wallets **leak each other's data** (shared positions) — dedup before trusting.
- Leaderboard 7d/30d windows are survivorship/variance, **not** proof of edge: one wallet showed +$116k/7d but **−$182k** over the full 15-day record.

## So what
The single profitable mechanism that the forensics, the calibration study, and the structural analysis **all** point back to is **liquidity-rewards market-making**. The "uncopyable" framing only applies to matching million-dollar MMs; at small scale the same edge is *under*-crowded (see the capacity discussion in [04](04-lp-rewards-edge.md)).
