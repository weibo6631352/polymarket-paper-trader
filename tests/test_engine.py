"""Tests for the trade execution engine."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pm_trader.db import Database
from pm_trader.engine import Engine
from pm_trader.models import (
    InsufficientBalanceError,
    InvalidOutcomeError,
    Market,
    MarketClosedError,
    NoPositionError,
    NotInitializedError,
    OrderBook,
    OrderBookLevel,
    OrderRejectedError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_data_dir: Path) -> Engine:
    eng = Engine(tmp_data_dir)
    yield eng
    eng.close()


@pytest.fixture
def initialized_engine(engine: Engine) -> Engine:
    """Engine with an initialized $10k account."""
    engine.init_account(10_000.0)
    return engine


def _make_book(
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
) -> OrderBook:
    """Helper to build an OrderBook from tuples."""
    return OrderBook(
        bids=[OrderBookLevel(price=p, size=s) for p, s in (bids or [])],
        asks=[OrderBookLevel(price=p, size=s) for p, s in (asks or [])],
    )


SAMPLE_MARKET = Market(
    condition_id="0xabc123",
    slug="will-bitcoin-hit-100k",
    question="Will Bitcoin hit $100k?",
    description="BTC market",
    outcomes=["Yes", "No"],
    outcome_prices=[0.65, 0.35],
    tokens=[
        {"token_id": "tok_yes", "outcome": "Yes"},
        {"token_id": "tok_no", "outcome": "No"},
    ],
    active=True,
    closed=False,
    volume=5_000_000.0,
    liquidity=250_000.0,
    end_date="2026-12-31",
    fee_rate_bps=0,
    tick_size=0.01,
)

SAMPLE_BOOK = _make_book(
    bids=[(0.64, 500), (0.63, 500), (0.62, 500)],
    asks=[(0.66, 500), (0.67, 500), (0.68, 500)],
)


def _mock_api(engine: Engine, market=None, book=None, fee_rate=0):
    """Patch the engine's API client methods."""
    m = market or SAMPLE_MARKET
    b = book or SAMPLE_BOOK
    engine.api.get_market = MagicMock(return_value=m)
    engine.api.get_trade_context = MagicMock(return_value=(m, b, fee_rate))
    engine.api.get_order_book = MagicMock(return_value=b)
    engine.api.get_fee_rate = MagicMock(return_value=fee_rate)
    engine.api.get_midpoint = MagicMock(return_value=0.65)


# ---------------------------------------------------------------------------
# Account tests
# ---------------------------------------------------------------------------


class TestAccount:
    def test_init_account(self, engine: Engine):
        account = engine.init_account(5000.0)
        assert account.cash == 5000.0
        assert account.starting_balance == 5000.0

    def test_get_account_not_initialized(self, engine: Engine):
        with pytest.raises(NotInitializedError):
            engine.get_account()

    def test_get_account_after_init(self, initialized_engine: Engine):
        account = initialized_engine.get_account()
        assert account.cash == 10_000.0

    def test_reset(self, initialized_engine: Engine):
        initialized_engine.reset()
        with pytest.raises(NotInitializedError):
            initialized_engine.get_account()


# ---------------------------------------------------------------------------
# Buy tests
# ---------------------------------------------------------------------------


class TestBuy:
    def test_basic_buy(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        result = initialized_engine.buy("will-bitcoin-hit-100k", "yes", 100.0)
        assert result.trade.side == "buy"
        assert result.trade.outcome == "yes"
        assert result.trade.amount_usd > 0
        assert result.trade.shares > 0
        assert result.account.cash < 10_000.0

    def test_buy_updates_position(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 100.0)
        pos = initialized_engine.db.get_position("0xabc123", "yes")
        assert pos is not None
        assert pos.shares > 0
        assert pos.total_cost > 0

    def test_buy_adds_to_existing_position(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 50.0)
        pos1 = initialized_engine.db.get_position("0xabc123", "yes")
        shares1 = pos1.shares

        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 50.0)
        pos2 = initialized_engine.db.get_position("0xabc123", "yes")
        assert pos2.shares > shares1

    def test_buy_no_outcome(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        result = initialized_engine.buy("will-bitcoin-hit-100k", "no", 100.0)
        assert result.trade.outcome == "no"

    def test_buy_insufficient_balance(self, initialized_engine: Engine):
        # Book has enough liquidity but account doesn't have enough cash
        deep_book = _make_book(
            bids=[(0.64, 100_000)],
            asks=[(0.66, 100_000)],  # $66k of liquidity
        )
        _mock_api(initialized_engine, book=deep_book)
        with pytest.raises(InsufficientBalanceError):
            initialized_engine.buy("btc", "yes", 50_000.0)

    def test_buy_invalid_outcome(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        with pytest.raises(InvalidOutcomeError):
            initialized_engine.buy("btc", "maybe", 100.0)

    def test_buy_below_minimum(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        with pytest.raises(OrderRejectedError, match="Minimum"):
            initialized_engine.buy("btc", "yes", 0.5)

    def test_buy_closed_market(self, initialized_engine: Engine):
        closed = Market(
            condition_id="0xclosed",
            slug="closed-market",
            question="Closed?",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[1.0, 0.0],
            tokens=[
                {"token_id": "t1", "outcome": "Yes"},
                {"token_id": "t2", "outcome": "No"},
            ],
            active=False,
            closed=True,
            fee_rate_bps=0,
            tick_size=0.01,
        )
        _mock_api(initialized_engine, market=closed)
        with pytest.raises(MarketClosedError):
            initialized_engine.buy("closed-market", "yes", 100.0)

    def test_buy_fok_rejected_insufficient_liquidity(self, initialized_engine: Engine):
        thin_book = _make_book(
            bids=[(0.64, 10)],
            asks=[(0.66, 10)],  # Only $6.60 of liquidity
        )
        _mock_api(initialized_engine, book=thin_book)
        with pytest.raises(OrderRejectedError, match="FOK"):
            initialized_engine.buy("btc", "yes", 100.0, order_type="fok")

    def test_buy_fak_partial_fill(self, initialized_engine: Engine):
        thin_book = _make_book(
            bids=[(0.64, 10)],
            asks=[(0.66, 10)],  # Only $6.60 of liquidity
        )
        _mock_api(initialized_engine, book=thin_book)
        result = initialized_engine.buy("btc", "yes", 100.0, order_type="fak")
        assert result.trade.is_partial is True
        assert result.trade.amount_usd < 100.0

    def test_buy_with_fees(self, initialized_engine: Engine):
        _mock_api(initialized_engine, fee_rate=200)
        result = initialized_engine.buy("btc", "yes", 100.0)
        assert result.trade.fee > 0
        assert result.trade.fee_rate_bps == 200

    def test_buy_deducts_cost_plus_fee(self, initialized_engine: Engine):
        _mock_api(initialized_engine, fee_rate=200)
        result = initialized_engine.buy("btc", "yes", 100.0)
        expected_cash = 10_000.0 - result.trade.amount_usd - result.trade.fee
        assert abs(result.account.cash - expected_cash) < 0.01

    def test_buy_records_multiple_levels(self, initialized_engine: Engine):
        multi_level_book = _make_book(
            bids=[(0.64, 50)],
            asks=[(0.66, 50), (0.67, 50)],  # Two levels
        )
        _mock_api(initialized_engine, book=multi_level_book)
        result = initialized_engine.buy("btc", "yes", 50.0)
        assert result.trade.levels_filled >= 1


# ---------------------------------------------------------------------------
# Sell tests
# ---------------------------------------------------------------------------


class TestSell:
    def _setup_position(self, engine: Engine):
        """Buy some shares first so we have a position to sell."""
        _mock_api(engine)
        engine.buy("will-bitcoin-hit-100k", "yes", 100.0)

    def test_basic_sell(self, initialized_engine: Engine):
        self._setup_position(initialized_engine)
        pos = initialized_engine.db.get_position("0xabc123", "yes")
        sell_shares = pos.shares / 2

        _mock_api(initialized_engine)
        result = initialized_engine.sell("will-bitcoin-hit-100k", "yes", sell_shares)
        assert result.trade.side == "sell"
        assert result.trade.shares == pytest.approx(sell_shares, abs=0.01)

    def test_sell_increases_cash(self, initialized_engine: Engine):
        self._setup_position(initialized_engine)
        cash_before = initialized_engine.get_account().cash

        pos = initialized_engine.db.get_position("0xabc123", "yes")
        _mock_api(initialized_engine)
        initialized_engine.sell("will-bitcoin-hit-100k", "yes", pos.shares / 2)
        cash_after = initialized_engine.get_account().cash
        assert cash_after > cash_before

    def test_sell_reduces_position(self, initialized_engine: Engine):
        self._setup_position(initialized_engine)
        pos_before = initialized_engine.db.get_position("0xabc123", "yes")

        _mock_api(initialized_engine)
        initialized_engine.sell("will-bitcoin-hit-100k", "yes", pos_before.shares / 2)
        pos_after = initialized_engine.db.get_position("0xabc123", "yes")
        assert pos_after.shares < pos_before.shares

    def test_sell_no_position(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        with pytest.raises(NoPositionError):
            initialized_engine.sell("will-bitcoin-hit-100k", "yes", 10.0)

    def test_sell_more_than_held(self, initialized_engine: Engine):
        self._setup_position(initialized_engine)
        pos = initialized_engine.db.get_position("0xabc123", "yes")

        _mock_api(initialized_engine)
        with pytest.raises(OrderRejectedError, match="Cannot sell"):
            initialized_engine.sell("will-bitcoin-hit-100k", "yes", pos.shares + 100)

    def test_sell_with_fees(self, initialized_engine: Engine):
        self._setup_position(initialized_engine)
        pos = initialized_engine.db.get_position("0xabc123", "yes")

        _mock_api(initialized_engine, fee_rate=175)
        result = initialized_engine.sell("will-bitcoin-hit-100k", "yes", pos.shares / 2)
        assert result.trade.fee > 0
        assert result.trade.fee_rate_bps == 175

    def test_sell_realized_pnl_tracked(self, initialized_engine: Engine):
        self._setup_position(initialized_engine)
        pos = initialized_engine.db.get_position("0xabc123", "yes")

        _mock_api(initialized_engine)
        initialized_engine.sell("will-bitcoin-hit-100k", "yes", pos.shares)
        pos_after = initialized_engine.db.get_position("0xabc123", "yes")
        # realized_pnl should be non-zero (could be profit or loss)
        assert pos_after.realized_pnl != 0.0 or pos_after.shares == 0


# ---------------------------------------------------------------------------
# Portfolio tests
# ---------------------------------------------------------------------------


class TestPortfolio:
    def test_empty_portfolio(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        portfolio = initialized_engine.get_portfolio()
        assert portfolio == []

    def test_portfolio_with_position(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 100.0)

        portfolio = initialized_engine.get_portfolio()
        assert len(portfolio) == 1
        assert portfolio[0]["outcome"] == "yes"
        assert portfolio[0]["shares"] > 0
        assert "unrealized_pnl" in portfolio[0]
        assert "live_price" in portfolio[0]

    def test_portfolio_not_initialized(self, engine: Engine):
        with pytest.raises(NotInitializedError):
            engine.get_portfolio()


# ---------------------------------------------------------------------------
# Balance tests
# ---------------------------------------------------------------------------


class TestBalance:
    def test_initial_balance(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        balance = initialized_engine.get_balance()
        assert balance["cash"] == 10_000.0
        assert balance["starting_balance"] == 10_000.0
        assert balance["positions_value"] == 0.0
        assert balance["total_value"] == 10_000.0
        assert balance["pnl"] == 0.0

    def test_balance_after_buy(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 100.0)

        balance = initialized_engine.get_balance()
        assert balance["cash"] < 10_000.0
        assert balance["positions_value"] > 0
        assert balance["total_value"] > 0


# ---------------------------------------------------------------------------
# History tests
# ---------------------------------------------------------------------------


class TestHistory:
    def test_empty_history(self, initialized_engine: Engine):
        trades = initialized_engine.get_history()
        assert trades == []

    def test_history_after_trades(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 50.0)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 50.0)

        trades = initialized_engine.get_history()
        assert len(trades) == 2
        # Newest first
        assert trades[0].id > trades[1].id


# ---------------------------------------------------------------------------
# Resolution tests
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_winning_position(self, initialized_engine: Engine):
        # Buy YES, then market resolves YES wins
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 100.0)
        pos = initialized_engine.db.get_position("0xabc123", "yes")
        shares = pos.shares

        # Now mock a resolved market where YES won
        resolved_market = Market(
            condition_id="0xabc123",
            slug="will-bitcoin-hit-100k",
            question="Will Bitcoin hit $100k?",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[1.0, 0.0],
            tokens=[
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ],
            active=False,
            closed=True,
            fee_rate_bps=0,
            tick_size=0.01,
        )
        initialized_engine.api.get_market = MagicMock(return_value=resolved_market)

        results = initialized_engine.resolve_market("will-bitcoin-hit-100k")
        assert len(results) == 1
        assert results[0].payout == pytest.approx(shares, abs=0.01)
        assert results[0].position.is_resolved is True

    def test_resolve_losing_position(self, initialized_engine: Engine):
        # Buy YES, but NO wins
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 100.0)

        resolved_market = Market(
            condition_id="0xabc123",
            slug="will-bitcoin-hit-100k",
            question="Will Bitcoin hit $100k?",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[0.0, 1.0],
            tokens=[
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ],
            active=False,
            closed=True,
            fee_rate_bps=0,
            tick_size=0.01,
        )
        initialized_engine.api.get_market = MagicMock(return_value=resolved_market)

        results = initialized_engine.resolve_market("will-bitcoin-hit-100k")
        assert len(results) == 1
        assert results[0].payout == 0.0

    def test_resolve_not_closed(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 100.0)
        # Market is still open
        with pytest.raises(MarketClosedError):
            initialized_engine.resolve_market("will-bitcoin-hit-100k")

    def test_resolve_no_position(self, initialized_engine: Engine):
        resolved_market = Market(
            condition_id="0xnone",
            slug="no-pos",
            question="?",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[1.0, 0.0],
            tokens=[
                {"token_id": "t1", "outcome": "Yes"},
                {"token_id": "t2", "outcome": "No"},
            ],
            active=False,
            closed=True,
            fee_rate_bps=0,
            tick_size=0.01,
        )
        initialized_engine.api.get_market = MagicMock(return_value=resolved_market)
        with pytest.raises(NoPositionError):
            initialized_engine.resolve_market("no-pos")

    def test_resolve_adds_payout_to_cash(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("will-bitcoin-hit-100k", "yes", 100.0)
        pos = initialized_engine.db.get_position("0xabc123", "yes")
        cash_before_resolve = initialized_engine.get_account().cash

        resolved_market = Market(
            condition_id="0xabc123",
            slug="will-bitcoin-hit-100k",
            question="Will Bitcoin hit $100k?",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[1.0, 0.0],
            tokens=[
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ],
            active=False,
            closed=True,
            fee_rate_bps=0,
            tick_size=0.01,
        )
        initialized_engine.api.get_market = MagicMock(return_value=resolved_market)
        initialized_engine.resolve_market("will-bitcoin-hit-100k")

        cash_after = initialized_engine.get_account().cash
        assert cash_after > cash_before_resolve


# ---------------------------------------------------------------------------
# Outcome validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_outcome_case_insensitive(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        result = initialized_engine.buy("btc", "YES", 50.0)
        assert result.trade.outcome == "yes"

    def test_outcome_whitespace_stripped(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        result = initialized_engine.buy("btc", " yes ", 50.0)
        assert result.trade.outcome == "yes"


class TestWatchPrices:
    def test_invalid_outcome_raises(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        with pytest.raises(ValueError, match="maybe"):
            initialized_engine.watch_prices(["btc"], ["maybe"])


class TestCheckOrdersRejection:
    def test_unfillable_order_gets_rejected(self, initialized_engine: Engine):
        """An order with amount below minimum should be rejected, not retried forever."""
        _mock_api(initialized_engine)
        # Bypass engine validation by inserting directly into orders table
        from pm_trader.orders import create_order, get_pending_orders

        # Sell order with no position — permanently unfillable (NoPositionError)
        order = create_order(
            initialized_engine.db.conn,
            market_slug="will-bitcoin-hit-100k",
            market_condition_id="0xabc123",
            outcome="yes",
            side="sell",
            amount=10.0,
            limit_price=0.50,  # Low limit so best_bid (0.64) >= limit
        )
        assert len(get_pending_orders(initialized_engine.db.conn)) == 1

        results = initialized_engine.check_orders()

        # Order should be rejected, not still pending
        assert len(results) >= 1
        rejected = [r for r in results if r["action"] == "rejected"]
        assert len(rejected) == 1
        assert rejected[0]["order"]["status"] == "rejected"
        assert "No position" in rejected[0]["reason"]

        # No pending orders left
        assert len(get_pending_orders(initialized_engine.db.conn)) == 0


class TestLimitOrderValidation:
    def test_gtd_without_expiry_rejected(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        with pytest.raises(OrderRejectedError, match="expires_at"):
            initialized_engine.place_limit_order(
                "btc", "yes", "buy", 100.0, 0.50,
                order_type="gtd", expires_at=None,
            )

    def test_buy_amount_below_minimum_rejected(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        with pytest.raises(OrderRejectedError, match="Minimum"):
            initialized_engine.place_limit_order(
                "btc", "yes", "buy", 0.50, 0.55,
            )


class TestLimitOrderPriceEnforcement:
    """Bug #1: Limit orders must NOT fill at prices beyond the limit."""

    def test_buy_limit_skips_asks_above_limit(self, initialized_engine: Engine):
        """A buy limit at 0.55 must NOT fill when all asks are above 0.55."""
        _mock_api(initialized_engine)
        from pm_trader.orders import create_order, get_pending_orders

        create_order(
            initialized_engine.db.conn,
            market_slug="will-bitcoin-hit-100k",
            market_condition_id="0xabc123",
            outcome="yes",
            side="buy",
            amount=100.0,
            limit_price=0.55,  # Below best ask (0.66)
        )
        results = initialized_engine.check_orders()
        # Should NOT fill — all asks are above 0.55
        filled = [r for r in results if r["action"] == "filled"]
        assert len(filled) == 0
        assert len(get_pending_orders(initialized_engine.db.conn)) == 1

    def test_buy_limit_fills_at_or_below_limit(self, initialized_engine: Engine):
        """A buy limit at 0.70 fills at asks 0.66, 0.67, 0.68 (all <= 0.70)."""
        _mock_api(initialized_engine)
        from pm_trader.orders import create_order, get_pending_orders

        create_order(
            initialized_engine.db.conn,
            market_slug="will-bitcoin-hit-100k",
            market_condition_id="0xabc123",
            outcome="yes",
            side="buy",
            amount=100.0,
            limit_price=0.70,  # Above best ask (0.66)
        )
        results = initialized_engine.check_orders()
        filled = [r for r in results if r["action"] == "filled"]
        assert len(filled) == 1
        assert len(get_pending_orders(initialized_engine.db.conn)) == 0


# ---------------------------------------------------------------------------
# Additional engine edge case tests (coverage gaps)
# ---------------------------------------------------------------------------


class TestValidateOutcome:
    def test_empty_outcome_raises(self, initialized_engine: Engine):
        with pytest.raises(InvalidOutcomeError):
            initialized_engine._validate_outcome("")

    def test_whitespace_only_raises(self, initialized_engine: Engine):
        with pytest.raises(InvalidOutcomeError):
            initialized_engine._validate_outcome("   ")


class TestSellEdgeCases:
    def test_sell_more_than_held(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("btc", "yes", 100.0)
        with pytest.raises(OrderRejectedError, match="Cannot sell"):
            initialized_engine.sell("btc", "yes", 99_999.0)

    def test_sell_closed_market(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("btc", "yes", 100.0)
        closed = Market(
            condition_id="0xabc123",
            slug="will-bitcoin-hit-100k",
            question="Q",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[1.0, 0.0],
            tokens=[
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ],
            active=False,
            closed=True,
        )
        initialized_engine.api.get_market = MagicMock(return_value=closed)
        with pytest.raises(MarketClosedError):
            initialized_engine.sell("btc", "yes", 10.0)

    def test_sell_fok_rejected_on_empty_book(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("btc", "yes", 100.0)
        empty_book = _make_book(bids=[], asks=[])
        initialized_engine.api.get_order_book = MagicMock(return_value=empty_book)
        with pytest.raises(OrderRejectedError, match="FOK rejected"):
            initialized_engine.sell("btc", "yes", 10.0)


class TestResolveMarket:
    def test_resolve_closed_market_with_winner(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        # Buy YES shares
        initialized_engine.buy("btc", "yes", 100.0)

        # Market resolves — YES wins
        resolved_market = Market(
            condition_id="0xabc123",
            slug="will-bitcoin-hit-100k",
            question="Q",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[1.0, 0.0],
            tokens=[
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ],
            active=False,
            closed=True,
        )
        initialized_engine.api.get_market = MagicMock(return_value=resolved_market)
        results = initialized_engine.resolve_market("btc")
        assert len(results) >= 1
        # YES payout should be $1/share
        yes_result = [r for r in results if r.position.outcome == "yes"]
        assert len(yes_result) == 1
        assert yes_result[0].payout > 0

    def test_resolve_not_closed(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        with pytest.raises(MarketClosedError, match="not yet closed"):
            initialized_engine.resolve_market("btc")

    def test_resolve_no_position(self, initialized_engine: Engine):
        closed = Market(
            condition_id="0xclosed",
            slug="closed-market",
            question="Q",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[1.0, 0.0],
            tokens=[
                {"token_id": "t1", "outcome": "Yes"},
                {"token_id": "t2", "outcome": "No"},
            ],
            active=False,
            closed=True,
        )
        initialized_engine.api.get_market = MagicMock(return_value=closed)
        with pytest.raises(NoPositionError):
            initialized_engine.resolve_market("closed-market")


class TestResolveAll:
    def test_resolve_all_with_closed_market(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        # Buy YES shares
        initialized_engine.buy("btc", "yes", 100.0)

        # Market resolves — YES wins
        resolved_market = Market(
            condition_id="0xabc123",
            slug="will-bitcoin-hit-100k",
            question="Q",
            description="",
            outcomes=["Yes", "No"],
            outcome_prices=[1.0, 0.0],
            tokens=[
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ],
            active=False,
            closed=True,
        )
        initialized_engine.api.get_market = MagicMock(return_value=resolved_market)
        results = initialized_engine.resolve_all()
        assert len(results) >= 1


class TestWatchPricesEdgeCases:
    def test_watch_market_not_found(self, initialized_engine: Engine):
        """Markets that can't be resolved are silently skipped."""
        from pm_trader.models import MarketNotFoundError
        initialized_engine.api.get_market = MagicMock(
            side_effect=MarketNotFoundError("bad")
        )
        result = initialized_engine.watch_prices(["bad"])
        assert result == []

    def test_watch_api_price_error(self, initialized_engine: Engine):
        """Price fetch errors are silently skipped for that market."""
        _mock_api(initialized_engine)
        initialized_engine.api.get_midpoint = MagicMock(side_effect=Exception("timeout"))
        result = initialized_engine.watch_prices(["btc"])
        assert result == []

    def test_watch_default_outcome(self, initialized_engine: Engine):
        """Without outcomes param, defaults to ['yes']."""
        _mock_api(initialized_engine)
        result = initialized_engine.watch_prices(["btc"])
        assert len(result) == 1
        assert result[0]["outcome"] == "yes"


class TestLimitOrderExecution:
    """Test the limit order fill execution paths."""

    def test_sell_limit_fills_when_bid_above_limit(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        # First buy shares
        initialized_engine.buy("btc", "yes", 100.0)
        from pm_trader.orders import create_order, get_pending_orders

        create_order(
            initialized_engine.db.conn,
            market_slug="will-bitcoin-hit-100k",
            market_condition_id="0xabc123",
            outcome="yes",
            side="sell",
            amount=10.0,
            limit_price=0.50,  # Below best bid (0.64), so should fill
        )
        results = initialized_engine.check_orders()
        filled = [r for r in results if r["action"] == "filled"]
        assert len(filled) == 1
        assert len(get_pending_orders(initialized_engine.db.conn)) == 0

    def test_sell_limit_skips_when_bid_below_limit(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.buy("btc", "yes", 100.0)
        from pm_trader.orders import create_order, get_pending_orders

        create_order(
            initialized_engine.db.conn,
            market_slug="will-bitcoin-hit-100k",
            market_condition_id="0xabc123",
            outcome="yes",
            side="sell",
            amount=10.0,
            limit_price=0.90,  # Above best bid (0.64), so should NOT fill
        )
        results = initialized_engine.check_orders()
        filled = [r for r in results if r["action"] == "filled"]
        assert len(filled) == 0
        assert len(get_pending_orders(initialized_engine.db.conn)) == 1


class TestCancelAllOrders:
    def test_cancel_all_empty(self, initialized_engine: Engine):
        result = initialized_engine.cancel_all_orders()
        assert result == []

    def test_cancel_all_with_orders(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        initialized_engine.place_limit_order("btc", "yes", "buy", 100.0, 0.55)
        initialized_engine.place_limit_order("btc", "yes", "buy", 200.0, 0.50)
        cancelled = initialized_engine.cancel_all_orders()
        assert len(cancelled) == 2
        assert initialized_engine.get_pending_orders() == []


class TestOrderTypeValidation:
    def test_invalid_order_type_rejected(self, initialized_engine: Engine):
        _mock_api(initialized_engine)
        with pytest.raises(OrderRejectedError, match="Invalid order_type"):
            initialized_engine.place_limit_order(
                "btc", "yes", "buy", 100.0, 0.55, order_type="bad",
            )


# ---------------------------------------------------------------------------
# Maker quotes (liquidity-rewards two-sided quoting)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

T0 = datetime(2026, 6, 24, 0, 0, 0, tzinfo=timezone.utc)

MAKER_POOL = {
    "daily": 100.0, "max_spread": 4.0, "min_size": 50.0, "tick": 0.01,
    "token": "tok_yes", "question": "Q?", "condition_id": "0xabc123",
}

# bids/asks 10c either side of a 0.50 mid → nothing inside a 4c band
OUT_OF_BAND_BOOK = _make_book(bids=[(0.40, 1000)], asks=[(0.60, 1000)])
# 1c either side of mid → competitors inside the band
CONTESTED_BOOK = _make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])


def _mock_maker_api(engine, market=None, pool="default", book=None, mid=0.50):
    """Patch the API surface used by maker-quote methods."""
    m = market or SAMPLE_MARKET
    engine.api.get_market = MagicMock(return_value=m)
    cfg = dict(MAKER_POOL) if pool == "default" else pool
    engine.api.get_reward_config = MagicMock(return_value=cfg)
    engine.api.get_order_book = MagicMock(
        return_value=book if book is not None else OUT_OF_BAND_BOOK
    )
    engine.api.get_midpoint = MagicMock(return_value=mid)


class TestPlaceMakerQuote:
    def test_place_reserves_capital(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        q = eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        assert q["status"] == "active"
        assert q["size"] == 50.0
        assert q["half_spread_c"] == pytest.approx(1.0)  # one tick default
        assert q["committed_capital"] == pytest.approx(49.0)
        assert q["daily_rate"] == 100.0
        assert q["last_mid"] == pytest.approx(0.50)
        # cash dropped by committed capital
        assert eng.get_account().cash == pytest.approx(10_000.0 - 49.0)

    def test_place_custom_size_and_spread(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        q = eng.place_maker_quote(
            "will-bitcoin-hit-100k", "yes", size=100.0, half_spread_cents=2.0, now=T0
        )
        assert q["size"] == 100.0
        assert q["half_spread_c"] == pytest.approx(2.0)
        assert q["committed_capital"] == pytest.approx(100.0 * (1 - 0.04))

    def test_size_below_min_rejected(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        with pytest.raises(OrderRejectedError):
            eng.place_maker_quote("will-bitcoin-hit-100k", "yes", size=40.0)

    def test_spread_too_wide_rejected(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        with pytest.raises(OrderRejectedError):
            eng.place_maker_quote(
                "will-bitcoin-hit-100k", "yes", half_spread_cents=5.0
            )

    def test_spread_nonpositive_rejected(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        with pytest.raises(OrderRejectedError):
            eng.place_maker_quote(
                "will-bitcoin-hit-100k", "yes", half_spread_cents=0.0
            )

    def test_not_in_rewards_program_rejected(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng, pool=None)
        with pytest.raises(OrderRejectedError):
            eng.place_maker_quote("will-bitcoin-hit-100k", "yes")

    def test_closed_market_rejected(self, initialized_engine: Engine, closed_market):
        eng = initialized_engine
        _mock_maker_api(eng, market=closed_market)
        with pytest.raises(MarketClosedError):
            eng.place_maker_quote("will-eth-hit-5k", "yes")

    def test_invalid_mid_rejected(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng, mid=0.0)
        with pytest.raises(OrderRejectedError):
            eng.place_maker_quote("will-bitcoin-hit-100k", "yes")

    def test_insufficient_cash_rejected(self, engine: Engine):
        engine.init_account(10.0)
        _mock_maker_api(engine)
        with pytest.raises(InsufficientBalanceError):
            engine.place_maker_quote("will-bitcoin-hit-100k", "yes")

    def test_invalid_outcome_rejected(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        with pytest.raises(InvalidOutcomeError):
            eng.place_maker_quote("will-bitcoin-hit-100k", "maybe")

    def test_requires_account(self, engine: Engine):
        _mock_maker_api(engine)
        with pytest.raises(NotInitializedError):
            engine.place_maker_quote("will-bitcoin-hit-100k", "yes")


class TestGetMakerQuotes:
    def test_lists_active(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        quotes = eng.get_maker_quotes()
        assert len(quotes) == 1
        assert quotes[0]["status"] == "active"


class TestCancelMakerQuote:
    def test_cancel_releases_capital(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        q = eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        assert eng.get_account().cash == pytest.approx(9951.0)
        cancelled = eng.cancel_maker_quote(q["id"])
        assert cancelled["status"] == "cancelled"
        assert eng.get_account().cash == pytest.approx(10_000.0)
        assert eng.get_maker_quotes() == []

    def test_cancel_missing_none(self, initialized_engine: Engine):
        assert initialized_engine.cancel_maker_quote(999) is None

    def test_cancel_already_cancelled_none(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        q = eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        eng.cancel_maker_quote(q["id"])
        assert eng.cancel_maker_quote(q["id"]) is None


class TestAccrueMakerRewards:
    def test_empty_band_full_day_reward(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)  # out-of-band book → share 1.0
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        results = eng.accrue_maker_rewards(now=T0 + timedelta(days=1))
        assert len(results) == 1
        assert results[0]["share"] == pytest.approx(1.0)
        assert results[0]["reward"] == pytest.approx(100.0)
        assert results[0]["bleed"] == 0.0
        # cash = 10000 - 49 committed + 100 reward
        assert eng.get_account().cash == pytest.approx(10_051.0)

    def test_contested_book_partial_share(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng, book=CONTESTED_BOOK)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        results = eng.accrue_maker_rewards(now=T0 + timedelta(days=1))
        # my qmin 28.125 / (28.125 + 56.25) = 1/3
        assert results[0]["share"] == pytest.approx(1.0 / 3.0)
        assert results[0]["reward"] == pytest.approx(100.0 / 3.0)

    def test_adverse_bleed_on_jump(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        # mid jumps 5c with no elapsed time → pure bleed, no reward
        eng.api.get_midpoint.return_value = 0.45
        results = eng.accrue_maker_rewards(now=T0)
        assert results[0]["reward"] == 0.0
        assert results[0]["bleed"] == pytest.approx(2.0)  # 50 * (0.05 - 0.01)
        assert results[0]["quote"]["fills"] == 1
        assert eng.get_account().cash == pytest.approx(9951.0 - 2.0)

    def test_transient_book_error_skipped(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        eng.api.get_order_book = MagicMock(side_effect=Exception("down"))
        assert eng.accrue_maker_rewards(now=T0 + timedelta(days=1)) == []
        # no cash change
        assert eng.get_account().cash == pytest.approx(9951.0)

    def test_invalid_mid_skipped(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        eng.api.get_midpoint.return_value = 0.0
        assert eng.accrue_maker_rewards(now=T0 + timedelta(days=1)) == []

    def test_no_active_quotes_empty(self, initialized_engine: Engine):
        assert initialized_engine.accrue_maker_rewards(now=T0) == []

    def test_clock_skew_no_negative_reward(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        # accrue with an earlier clock → seconds clamped to 0
        results = eng.accrue_maker_rewards(now=T0 - timedelta(days=1))
        assert results[0]["reward"] == 0.0


class TestMakerSummaryAndBalance:
    def test_summary_aggregates(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        eng.accrue_maker_rewards(now=T0 + timedelta(days=1))
        s = eng.get_maker_summary()
        assert s["active_quotes"] == 1
        assert s["total_quotes"] == 1
        assert s["committed_capital"] == pytest.approx(49.0)
        assert s["reward_income"] == pytest.approx(100.0)
        assert s["adverse_bleed"] == 0.0
        assert s["net_maker_pnl"] == pytest.approx(100.0)

    def test_balance_reflects_maker(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        eng.accrue_maker_rewards(now=T0 + timedelta(days=1))
        bal = eng.get_balance()
        assert bal["maker_committed_capital"] == pytest.approx(49.0)
        assert bal["maker_reward_income"] == pytest.approx(100.0)
        assert bal["maker_net_pnl"] == pytest.approx(100.0)
        # total_value = cash(10051) + positions(0) + committed(49)
        assert bal["total_value"] == pytest.approx(10_100.0)
        assert bal["pnl"] == pytest.approx(100.0)

    def test_snapshot_equity_includes_committed(self, initialized_engine: Engine):
        eng = initialized_engine
        _mock_maker_api(eng)
        eng.place_maker_quote("will-bitcoin-hit-100k", "yes", now=T0)
        equity = eng.snapshot_equity()
        # cash 9951 + committed 49 = 10000 (no positions)
        assert equity == pytest.approx(10_000.0)
