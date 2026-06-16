"""Tests for the mark-to-market equity curve and the init_account clean-slate fix.

Covers:
- db: equity_curve recording / retrieval / clearing on init+reset
- engine: _book_mid, _record_equity (incl. best-effort failure), snapshot_equity
- analytics: equity-based Sharpe and max-drawdown
- regression: init_account is a full reset (no phantom P&L from stale positions)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pm_trader.analytics import (
    compute_stats,
    max_drawdown_from_equity,
    sharpe_ratio_from_equity,
)
from pm_trader.db import Database
from pm_trader.engine import Engine
from pm_trader.models import Account, Market, OrderBook, OrderBookLevel


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _book(bids, asks) -> OrderBook:
    return OrderBook(
        bids=[OrderBookLevel(price=p, size=s) for p, s in bids],
        asks=[OrderBookLevel(price=p, size=s) for p, s in asks],
    )


@pytest.fixture
def market() -> Market:
    return Market(
        condition_id="0xabc",
        slug="will-x-happen",
        question="Will X happen?",
        description="",
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        tokens=[
            {"token_id": "tok_yes", "outcome": "Yes"},
            {"token_id": "tok_no", "outcome": "No"},
        ],
        active=True,
        closed=False,
        fee_rate_bps=0,
        tick_size=0.01,
    )


@pytest.fixture
def engine(tmp_data_dir: Path) -> Engine:
    eng = Engine(tmp_data_dir)
    yield eng
    eng.close()


def _mock(engine: Engine, market: Market, book: OrderBook, mid: float = 0.5) -> None:
    engine.api.get_market = MagicMock(return_value=market)
    engine.api.get_order_book = MagicMock(return_value=book)
    engine.api.get_fee_rate = MagicMock(return_value=0)
    engine.api.get_midpoint = MagicMock(return_value=mid)


# ---------------------------------------------------------------------------
# db: equity_curve table
# ---------------------------------------------------------------------------

class TestEquityCurveDB:
    def test_record_and_get(self, tmp_data_dir: Path) -> None:
        db = Database(tmp_data_dir)
        db.init_schema()
        assert db.get_equity_curve() == []
        db.record_equity(10_000.0)
        db.record_equity(10_120.5)
        assert db.get_equity_curve() == [10_000.0, 10_120.5]

    def test_schema_has_equity_table(self, tmp_data_dir: Path) -> None:
        db = Database(tmp_data_dir)
        db.init_schema()
        names = [r["name"] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "equity_curve" in names

    def test_reset_drops_equity(self, tmp_data_dir: Path) -> None:
        db = Database(tmp_data_dir)
        db.init_schema()
        db.record_equity(10_000.0)
        db.reset()
        assert db.get_equity_curve() == []


# ---------------------------------------------------------------------------
# Regression: init_account is a full clean reset
# ---------------------------------------------------------------------------

class TestInitAccountCleanSlate:
    def test_init_clears_stale_state(self, engine: Engine, market: Market) -> None:
        engine.init_account(10_000.0)
        book = _book(bids=[(0.49, 500)], asks=[(0.51, 500)])
        _mock(engine, market, book)
        engine.buy("will-x-happen", "yes", 100.0)

        # State exists after a trade
        assert engine.get_history() != []
        assert engine.db.get_open_positions() != []
        assert engine.db.get_equity_curve() != []

        # Re-initializing must produce a truly flat account — no phantom P&L
        engine.init_account(10_000.0)
        assert engine.get_history() == []
        assert engine.db.get_open_positions() == []
        bal = engine.get_balance()
        assert bal["cash"] == pytest.approx(10_000.0)
        assert bal["positions_value"] == pytest.approx(0.0)
        assert bal["pnl"] == pytest.approx(0.0)
        # equity curve reseeded with exactly the starting (flat) equity
        assert engine.db.get_equity_curve() == [pytest.approx(10_000.0)]


# ---------------------------------------------------------------------------
# engine: equity recording hooks
# ---------------------------------------------------------------------------

class TestEngineEquityRecording:
    def test_init_seeds_starting_equity(self, engine: Engine) -> None:
        engine.init_account(10_000.0)
        assert engine.db.get_equity_curve() == [pytest.approx(10_000.0)]

    def test_buy_records_marked_equity(self, engine: Engine, market: Market) -> None:
        engine.init_account(10_000.0)
        book = _book(bids=[(0.49, 1000)], asks=[(0.50, 1000)])
        _mock(engine, market, book)
        engine.buy("will-x-happen", "yes", 100.0)
        curve = engine.db.get_equity_curve()
        # starting point + post-buy point; marked at book mid ~0.495 -> ~flat
        assert len(curve) == 2
        assert curve[-1] == pytest.approx(10_000.0, abs=1.0)

    def test_book_mid_none_falls_back_to_fill_price(
        self, engine: Engine, market: Market
    ) -> None:
        engine.init_account(10_000.0)
        # Asks present, bids empty -> _book_mid returns None -> uses fill price
        book = _book(bids=[], asks=[(0.50, 1000)])
        _mock(engine, market, book)
        engine.buy("will-x-happen", "yes", 100.0, order_type="fak")
        assert len(engine.db.get_equity_curve()) == 2

    def test_record_equity_is_best_effort(self, engine: Engine) -> None:
        engine.init_account(10_000.0)
        engine.db.record_equity = MagicMock(side_effect=RuntimeError("boom"))
        # Must not raise despite the bookkeeping failure
        engine._record_equity()

    def test_record_equity_noop_without_account(self, engine: Engine) -> None:
        # No init_account -> get_account() is None -> records nothing, no raise
        engine._record_equity()
        assert engine.db.get_equity_curve() == []

    def test_snapshot_equity_live_mark(self, engine: Engine, market: Market) -> None:
        engine.init_account(10_000.0)
        book = _book(bids=[(0.49, 1000)], asks=[(0.50, 1000)])
        _mock(engine, market, book, mid=0.80)
        engine.buy("will-x-happen", "yes", 100.0)
        shares = engine.db.get_open_positions()[0].shares
        eq = engine.snapshot_equity()
        bal = engine.get_account()
        assert eq == pytest.approx(bal.cash + shares * 0.80)
        assert engine.db.get_equity_curve()[-1] == pytest.approx(eq)

    def test_snapshot_equity_falls_back_when_price_unavailable(
        self, engine: Engine, market: Market
    ) -> None:
        engine.init_account(10_000.0)
        book = _book(bids=[(0.49, 1000)], asks=[(0.50, 1000)])
        _mock(engine, market, book, mid=0.50)
        engine.buy("will-x-happen", "yes", 100.0)
        pos = engine.db.get_open_positions()[0]
        # midpoint raises -> fall back to avg_entry_price
        engine.api.get_midpoint = MagicMock(side_effect=RuntimeError("no price"))
        eq = engine.snapshot_equity()
        assert eq == pytest.approx(engine.get_account().cash + pos.shares * pos.avg_entry_price)

    def test_snapshot_equity_falls_back_on_zero_price(
        self, engine: Engine, market: Market
    ) -> None:
        engine.init_account(10_000.0)
        book = _book(bids=[(0.49, 1000)], asks=[(0.50, 1000)])
        _mock(engine, market, book, mid=0.50)
        engine.buy("will-x-happen", "yes", 100.0)
        pos = engine.db.get_open_positions()[0]
        engine.api.get_midpoint = MagicMock(return_value=0.0)
        eq = engine.snapshot_equity()
        assert eq == pytest.approx(engine.get_account().cash + pos.shares * pos.avg_entry_price)


# ---------------------------------------------------------------------------
# analytics: equity-based Sharpe and drawdown
# ---------------------------------------------------------------------------

class TestEquityAnalytics:
    def test_sharpe_empty_or_single(self) -> None:
        assert sharpe_ratio_from_equity([]) == 0.0
        assert sharpe_ratio_from_equity([10_000.0]) == 0.0

    def test_sharpe_too_few_valid_returns(self) -> None:
        # leading non-positive equity filters the only pair -> <2 returns
        assert sharpe_ratio_from_equity([0.0, 100.0]) == 0.0

    def test_sharpe_zero_volatility(self) -> None:
        # constant per-period return -> std 0 -> Sharpe 0
        assert sharpe_ratio_from_equity([100.0, 110.0, 121.0]) == 0.0

    def test_sharpe_normal(self) -> None:
        s = sharpe_ratio_from_equity([100.0, 110.0, 105.0, 120.0])
        assert isinstance(s, float)
        assert s != 0.0

    def test_drawdown_empty(self) -> None:
        assert max_drawdown_from_equity([]) == 0.0

    def test_drawdown_rises_then_falls(self) -> None:
        # peak 120, trough 90 -> dd = 30/120 = 0.25
        assert max_drawdown_from_equity([100, 120, 90, 110]) == pytest.approx(0.25)

    def test_compute_stats_uses_equity_curve(self) -> None:
        acct = Account(id=1, starting_balance=10_000, cash=10_000, created_at="t")
        curve = [10_000, 10_300, 9_900, 10_500]
        stats = compute_stats([], acct, positions_value=0.0, equity_curve=curve)
        assert stats["sharpe_ratio"] == pytest.approx(sharpe_ratio_from_equity(curve))
        assert stats["max_drawdown"] == pytest.approx(max_drawdown_from_equity(curve))

    def test_compute_stats_falls_back_without_curve(self) -> None:
        acct = Account(id=1, starting_balance=10_000, cash=10_000, created_at="t")
        stats = compute_stats([], acct, positions_value=0.0)
        # no curve -> legacy path still yields numeric metrics
        assert stats["sharpe_ratio"] == 0.0
        assert stats["max_drawdown"] == 0.0
