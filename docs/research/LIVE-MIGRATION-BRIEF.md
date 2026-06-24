# Live Maker — Clean Migration Brief

Goal: a **new, standalone, lean repo** that runs the validated liquidity-rewards
market-making strategy **live on a real wallet**, autonomously. **No MCP, no paper
engine, no redundancy.** Built on the **official unified SDK `polymarket-client`
(`AsyncSecureClient`)** per https://docs.polymarket.com/dev-tooling/python.
Efficiency-first: async, WebSocket-driven, one process, no LLM in the hot path.

## 1. MIGRATE (copy the validated pure/decision logic from polymarket-paper-trader)
All engine-free. Source files in the old repo:
- **reward/bleed math** — `pm_trader/orderbook.py`: `_inband_weight`, `book_inband_qmin`,
  `maker_quote_score`, `maker_reward_share`, `reward_accrual`, `adverse_bleed`,
  `committed_capital`, `expected_excess_move`, `realized_sigma_c_from_history`,
  `optimal_half_spread`. → new `reward_math.py` (pure, copy as-is).
- **pool discovery + scoring** — `pm_trader/rewards.py`: `parse_rewards`, `inband_score`,
  `classify_jump_risk`, `score_pool`, `scan`, paginated `/sampling-markets`. → new
  `scanner.py` (keep a thin httpx client for the public CLOB read endpoints
  `/sampling-markets` `/book` `/prices-history`; SDK doesn't need to wrap these).
- **periodic refresh** — `pm_trader/discovery.py`: `refresh`/`watch`. → `discovery.py`.
- **book selection** — `pm_trader/portfolio.py`: `select_pools` (risk-adjusted yield +
  correlation-cluster dedup + cooldown + capital budget), `_cluster_key`,
  `_significant_tokens`, `_risk_adjusted_score`. → `portfolio.py`.
- **per-pool quoting/risk DECISIONS** — `pm_trader/maker_live.py`:
  `compute_two_sided_quotes` (skew), inventory tracking + skew, jump-halt + cooldown,
  re-quote logic. → `strategy.py` (keep the decision logic; drop the dry-run submitter).
- **reconcile + jump-exit DECISIONS** (currently inside `engine.accrue_maker_rewards`):
  re-check reward config each loop → cancel if pool left program / resolved; exit on a
  move beyond the band; use the CURRENT daily rate. → fold into `strategy.py`/`runner.py`.
- **research/economics** — `docs/research/*.md` (read-only context; do NOT re-derive).

## 2. EXCLUDE (the redundancy — do NOT carry over)
- `mcp_server.py` (agent interface — live is autonomous, no LLM/MCP in the loop).
- `engine.py`, `db.py`, `orders.py` (SQLite paper accrual/positions — live uses REAL
  fills from the WS user channel, not simulated accrual).
- `cli.py` (paper Click CLI), `analytics.py`, `card.py`, `benchmark.py`, `backtest.py`,
  `smartmoney.py`.
- `maker_sim.py` (backtester) — optional, keep ONLY as an offline tool, never in the
  live runtime.
- The paper account/balance abstraction entirely.

## 3. NEW live execution layer (official `polymarket-client`)
```bash
pip install polymarket-client   # or: uv add polymarket-client
```
```python
from polymarket import AsyncSecureClient
client = await AsyncSecureClient.create(
    private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
    wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
)
await client.setup_trading_approvals()          # wallet deploy + USDC approvals (once)

# quote
r = await client.place_limit_order(token_id=tok, side="BUY", price="0.49", size="50")
# or: order = await client.create_limit_order(...); r = await client.post_order(order)
if r.ok: order_id = r.order_id

# cancel (the latency-sensitive op — the fast-cancel edge)
await client.cancel_order(order_id=order_id)
await client.cancel_market_orders(token_id=tok)

# market data
book = await client.get_order_book(token_id=tok)
mid  = await client.get_midpoint(token_id=tok)

# real-time (WS) — drives the loop; no polling
from polymarket.streams import MarketSpec
stream = await client.subscribe([MarketSpec(token_ids=[tok, ...])])
async with stream:
    async for event in stream:   # MarketBookEvent / MarketPriceChangeEvent / fills
        ...
```
Wrap the client in `async with` or close it explicitly (it owns network transports).

## 4. Lean architecture (one async process)
```
config.py     pools/capital/risk params + env (private key, wallet)
reward_math.py  pure math (migrated)
scanner.py    thin httpx CLOB reader + score_pool/jump-risk (migrated)
discovery.py  periodic SAFE-pool refresh (migrated)
portfolio.py  select_pools risk-adjusted+decorrelated+cooldown (migrated)
strategy.py   per-pool decisions: quote (skew), inventory, jump-exit, reconcile (migrated)
execution.py  AsyncSecureClient adapter: place/cancel/approvals  (NEW, official SDK)
feed.py       client.subscribe(...) → normalized book/mid/fill events  (NEW)
runner.py     async loop: discover→select→subscribe→on-event(re-quote/cancel/inventory/
              jump-exit/reconcile); periodic discovery refresh; kill-switch  (NEW)
```
WebSocket-driven (react on events, not 120s polls). Minimal deps: `polymarket-client`,
`httpx`. Tests mirror source; keep the pure logic 100%-covered.

## 5. Safety / real-money gating (operator-owned)
- `POLYMARKET_PRIVATE_KEY` from env only; `.gitignore` `.env`. Never commit keys.
- Stage the ramp: start tiny (min_size, few pools), confirm jump-survival over days,
  then scale capital. Hard kill-switch + max-loss/day + inventory caps in `runner.py`.
- The validated economics (see `docs/research/04-lp-rewards-edge.md`): edge is thin +
  capacity-constrained; net-positive in calm windows; a catalyst jump can erase days;
  in CALM markets colocation adds ~0% (optimal-width quoting already neutralizes bleed)
  — proximity mainly pays for the separate latency-arb sleeve, not the MM. Don't quote
  tight chasing share unless you can cancel fast.

## 6. Kickoff (paste into the new session)
> New clean repo `polymarket-live-maker` (its own git, fresh). Build the autonomous
> live liquidity-rewards maker on the official `polymarket-client` SDK
> (https://docs.polymarket.com/dev-tooling/python), migrating ONLY the pure/decision
> logic listed in `polymarket-paper-trader/docs/research/LIVE-MIGRATION-BRIEF.md`
> (reward_math, scanner, discovery, portfolio, strategy), excluding all MCP/paper-engine
> code. Wire `execution.py`/`feed.py`/`runner.py` to `AsyncSecureClient`. Efficiency-first,
> WebSocket-driven, no LLM in the hot path. Keep `.env` (private key) gitignored. Start in
> dry-run/tiny-size; real-money go-live is the operator's explicit switch.
