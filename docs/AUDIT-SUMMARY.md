# Audit summary (2026-06-16)

Stage summary of a read-only/paper audit. No real orders, no funds, nothing pushed.
Profitability detail in [FINDINGS.md](./FINDINGS.md).

## 1. Final verdict on the repo

A **credible Polymarket paper-trading simulator + 30-tool MCP server, worth keeping.**
Faithful execution (real Gamma/CLOB order books, exact fee formula, level-by-level
fills, slippage, limit-order state machine, neg-risk resolution). 677 tests, 100%
coverage. No fakery, no telemetry, no keys, structurally cannot touch real money.
Excellent as an agent sandbox / backtesting tool — **not** a money-maker.

## 2. The only real edge — neg-risk settlement arbitrage

Buy 1 YES share on **every** candidate of a mutually-exclusive + exhaustive
("neg-risk") event when Σ(YES asks) < \$1; exactly one resolves YES → guaranteed
\$1/share at settlement.

- **Capacity (measured live):** ~\$1,360 total deployable across ~3 viable events.
- **Profit:** ~\$16.5 guaranteed (+1.2% on deployed).
- **Redemption:** realized only at event resolution (capital locked ~205 days weighted).
- **Annualized ≈ 2.2% — below the risk-free rate.** Real and risk-free, but
  capacity-bound and below cash. Bottleneck is capacity, not latency.

## 3. Trading strategies — measured paper P&L (live books)

| Strategy | Type | Result |
|---|---|---|
| Momentum | taker | **−33.0%**, 44 trades, 0% win, Sharpe −0.14, 33% max DD |
| EWMA mean-reversion | taker | **flat** (0 trades — signal never fired) |
| Passive market-making | limit | **flat** (0 fills — sim has no phantom flow) |
| Tick-scalper | limit | **flat** (0 fills) |

Takers bleed the spread (~−30% median round-trip); makers never fill. No scalable
positive edge.

## 4. Bugs fixed (committed locally, branch `fix/pnl-accounting`)

1. **`init_account` was not a clean reset** — reset cash but kept stale
   trades/positions → phantom P&L. Now clears trades/positions/equity_curve/limit_orders.
2. **Sharpe & max-drawdown used trade cash flows** (counts a buy as a loss, ignores
   open-position value). Added a mark-to-market `equity_curve` (per-trade snapshots +
   `snapshot_equity()` for live sampling); metrics now compute from the equity curve.

Also observed but **not** fixed: MCP server ignores `PM_TRADER_DATA_DIR`; the example
backtest silently makes 0 trades on synthetic slugs (`get_market` unpatched);
`benchmark run examples.*` needs `PYTHONPATH=.`.

## 5. One-line recommendation

Keep and maintain it as a credible agent sandbox / backtesting tool — don't invest in
chasing trading profit, since the only genuine edge (neg-risk arb) is capacity-bound
and sub-cash unless Polymarket fees/liquidity change materially.
