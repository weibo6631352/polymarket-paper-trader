# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# polymarket-paper-trader

Paper trading simulator for Polymarket. Built for AI agents. Python 3.10+, SQLite, Click CLI, FastMCP.

## Commands

```bash
# Install
pip install -e ".[dev]"

# Tests (883 non-live + 42 live = 925 total, 100% coverage)
python3 -m pytest tests/ -x -q -m "not live"            # fast, skip live API tests
python3 -m pytest tests/ -v                              # verbose
python3 -m pytest tests/ --cov=pm_trader --cov-report=term-missing  # coverage
python3 -m pytest tests/test_e2e_live.py -v              # live API (requires network)

# Single test file / single test
python3 -m pytest tests/test_engine.py -x -q             # one file
python3 -m pytest tests/test_engine.py::TestBuy::test_buy_yes -x -q  # one test

# Run
pm-trader init --balance 10000
pm-trader-mcp                                            # MCP server on stdio
```

## Architecture

```
cli.py → engine.py → api.py (Polymarket HTTP)
                   → db.py (SQLite, WAL mode)
                   → orderbook.py (fill simulation + maker-reward math)
                   → orders.py (limit order + maker-quote state machine)

mcp_server.py → engine.py (trading tools, 38 MCP tools)
              → analytics.py, card.py, benchmark.py, backtest.py, smartmoney.py, rewards.py (lazy imports)
```

- **Engine** is the orchestrator. All trading logic goes through it.
- **mcp_server.py** uses engine for trading, but imports analytics/card/benchmark/backtest/rewards directly (lazy, inside tool functions, to keep startup fast).
- **orderbook.py** has pure functions (`simulate_buy_fill`, `simulate_sell_fill`) — no side effects. Also the pure maker-reward math (`maker_reward_share`, `reward_accrual`, `adverse_bleed`, `committed_capital`, `book_inband_qmin`) that the engine's maker quotes accrue on.
- **orders.py** has pure SQLite functions for limit order CRUD *and* maker-quote CRUD (`maker_quotes` table) — no Engine dependency.
- **api.py** talks to Gamma API (market discovery) and CLOB API (prices, order books, `get_reward_config` for a market's liquidity-reward pool).
- **smartmoney.py** talks to lb-api (real-trader leaderboard) and data-api (trader positions/value) — copy-trading scanner, no Engine/db dependency. Surfaces consistent traders' fresh positions for de-vig evaluation; does not decide trades.
- **rewards.py** scans Polymarket's liquidity-rewards program (CLOB `/sampling-markets`, `/book`, `/prices-history`) and ranks pools by gross yield + jump risk — no Engine/db dependency. Surfaces pools; does not place orders.
- **maker_sim.py** is a standalone backtester for the maker-rewards edge: replays a midpoint price path (synthetic or real CLOB history) and reports `net = reward_income − adverse_bleed` with a `cancel_efficiency` (colocation) lever — no Engine/db dependency. Use it to validate expected yield; use the engine's maker quotes to paper-trade it statefully over time.
- **maker_live.py** is the live-quoting bridge: `LiveMakerBot` computes the two-sided orders to rest in a pool and the cancel/re-quote actions as mid moves (the fast-cancel that colocation accelerates). **Real money is hard-gated** — `dry_run=True` default submits nothing; `build_clob_signer` refuses unless `PM_TRADER_LIVE=1` + a private key + py-clob-client are present. CLI `maker plan` runs a read-only dry-run against the live book. No keys are held here; the operator wires the signer and flips the switch.
- **db.py** owns the SQLite schema. WAL mode for concurrent reads.

## Conventions

### Code style
- `from __future__ import annotations` at top of every module
- Complete type hints on all functions: `def foo(x: int) -> str:`
- Union types use `|` syntax: `str | None`, not `Optional[str]`
- Private functions prefixed with `_`: `_parse_market()`, `_get_cached()`
- Outcomes always lowercase: `"yes"`, `"no"` (normalized via `_validate_outcome`)

### Error handling
- Custom hierarchy: `SimError` → `InsufficientBalanceError`, `MarketClosedError`, etc.
- Each error has a `code` class attribute: `code = "INSUFFICIENT_BALANCE"`
- CLI and MCP use JSON envelope: `{"ok": true, "data": {...}}` or `{"ok": false, "error": "msg", "code": "CODE"}`
- Helper functions: `_ok(data)` and `_err(error, code)` in both cli.py and mcp_server.py
- `_err_from(e)` in mcp_server.py: wraps exceptions — exposes `SimError`/`ValueError`/`TypeError` messages, sanitizes everything else to `"Internal error"`

### Shared helpers
- `_market_to_dict(m)` in mcp_server.py — single serializer for Market→dict (used by all market-returning tools)
- `_parse_market_list(data)` in api.py — shared parser for Gamma API market list responses
- Don't cache empty API responses — guard with `len(data) > 0` before `_set_cached()`

### Security
- Account names validated via `_validate_account_name()`: rejects `..`, `/`, `\`, empty, leading/trailing whitespace
- `MAX_RESULTS = 100` caps all market-listing tool limits to prevent resource exhaustion

### Key design decisions
- **Fee formula**: `(bps/10000) * min(price, 1-price) * shares` — matches Polymarket exactly
- **FOK** (fill-or-kill): all or nothing. **FAK** (fill-and-kill): partial fills ok
- **Limit orders**: GTC (rest until filled/cancelled) or GTD (expire at timestamp)
- **No price/book caching**: always live from API. Market metadata cached 5 min.
- **Multi-account**: separate SQLite databases at `~/.pm-trader/<account>/paper.db`
- **Maker quotes (liquidity rewards)**: a `maker_quotes` two-sided resting quote rests `half_spread_c` cents either side of mid with `size` shares per side. `accrue_maker_rewards()` (poll like `check_orders`) banks a share of the pool's daily USDC rate over in-band uptime and charges adverse bleed `size*(|Δmid| − offset)` when the mid jumps the offset. Reward share = own Qmin / (own + book Qmin), Qmin = binding (lighter) side, size-weighted `((c−s)/c)²`. Capital `size*(1−2s)` is reserved out of cash (added back into `total_value`). Reward income is reported separately from trading P&L in `stats` (`reward_income`, `adverse_bleed`, `net_maker_pnl`, `trading_pnl`). Pool config (`max_spread`, `min_size`, daily rate, tick) comes from `api.get_reward_config` / `rewards.parse_rewards`. Paper-only — no real CLOB signed-order execution.

## Testing rules

- **Always run tests after changes**: `python3 -m pytest tests/ -x -q -m "not live"`
- **Update tests in the same pass** as bug fixes or refactors. A change is not done until tests pass.
- **100% coverage is maintained.** New code must include tests. Use `pragma: no cover` only for `if __name__ == "__main__"` guards.
- Test files mirror source: `pm_trader/engine.py` → `tests/test_engine.py`
- Behavior tests in `test_behavior.py`: test from an agent's perspective (full workflows, not internals)
- E2E live tests in `test_e2e_live.py`: use `pytest.skip()` when live API data is unavailable
- Shared fixtures in `conftest.py`: `tmp_data_dir`, `sample_market`, `closed_market`, `sample_order_book`
- Mock API in behavior tests with `_mock(engine, market=..., book=..., fee=...)` helper
- Use `pytest.approx()` for float comparisons, `pytest.raises(ErrorType)` for exceptions

## Git rules

- Atomic commits: one logical change per commit
- Run tests before committing
- If rebase fails twice, reset and cherry-pick instead
