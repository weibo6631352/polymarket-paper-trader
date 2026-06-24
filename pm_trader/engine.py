"""Trade execution engine for pm-trader.

Orchestrates the full buy/sell/resolve workflow by wiring together
the API client, order book simulator, and database layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pm_trader.api import PolymarketClient
from pm_trader.db import Database
from pm_trader.models import (
    Account,
    AmbiguousResolutionError,
    ApiError,
    InsufficientBalanceError,
    InvalidOutcomeError,
    MarketClosedError,
    NoPositionError,
    NotInitializedError,
    OrderRejectedError,
    Position,
    ResolveResult,
    Trade,
    TradeResult,
)
from pm_trader.orders import (
    cancel_all_orders as _cancel_all_orders,
    cancel_maker_quote as _cancel_maker_quote,
    cancel_order,
    create_maker_quote,
    create_order,
    expire_orders,
    get_active_maker_quotes,
    get_all_maker_quotes,
    get_maker_quote,
    get_pending_orders,
    init_orders_schema,
    mark_filled,
    reject_order,
    should_fill,
    update_maker_quote_accrual,
)
from pm_trader.orderbook import (
    book_inband_qmin,
    committed_capital,
    maker_fill,
    maker_reward_share,
    optimal_half_spread,
    realized_sigma_c_from_history,
    reward_accrual,
    simulate_buy_fill,
    simulate_sell_fill,
)

MIN_ORDER_USD = 1.0  # Polymarket minimum order size

# Maker-quote inventory defaults: a two-sided quote that accumulates inventory on
# one-sided fills needs a position cap and an inventory skew, or a persistent
# trend runs it over (verified: uncapped → −$1080 vs capped+skew+exit → −$3 on
# the same uptrend).  See [[mm-principles-for-pm-rewards]].
MAKER_CAP_MULT = 4.0       # default position cap = 4 × quote size (shares)
MAKER_SKEW_STRENGTH = 1.0  # default inventory-skew lean (offsets per size-unit)

# Errors that indicate an order is permanently unfillable (not transient)
_PERMANENT_ORDER_ERRORS = (
    OrderRejectedError,
    InsufficientBalanceError,
    InvalidOutcomeError,
    MarketClosedError,
    NoPositionError,
)


class Engine:
    """Paper trading engine — 1:1 faithful to Polymarket execution."""

    def __init__(self, data_dir: Path) -> None:
        self.db = Database(data_dir)
        self.db.init_schema()
        init_orders_schema(self.db.conn)
        self.api = PolymarketClient(self.db)

    def close(self) -> None:
        self.api.close()
        self.db.close()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def init_account(self, balance: float = 10_000.0) -> Account:
        account = self.db.init_account(balance)
        # Seed the equity curve with the starting (flat) equity.
        self._record_equity()
        return account

    def get_account(self) -> Account:
        account = self.db.get_account()
        if account is None:
            raise NotInitializedError()
        return account

    def reset(self) -> None:
        self.db.reset()
        init_orders_schema(self.db.conn)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _require_account(self) -> Account:
        return self.get_account()

    @staticmethod
    def _validate_outcome(outcome: str, market=None) -> str:
        """Validate and normalize outcome against the market's actual outcomes.

        When market is provided, verifies the outcome exists in that market.
        Without market, only normalizes (caller is responsible for validation).
        """
        outcome = outcome.lower().strip()
        if not outcome:
            raise InvalidOutcomeError(outcome)
        if market is not None:
            valid = [o.lower() for o in market.outcomes]
            if outcome not in valid:
                raise InvalidOutcomeError(outcome, valid)
        return outcome

    # ------------------------------------------------------------------
    # BUY — spend USD, receive shares
    # ------------------------------------------------------------------

    def buy(
        self,
        slug_or_id: str,
        outcome: str,
        amount_usd: float,
        order_type: str = "fok",
    ) -> TradeResult:
        """Execute a buy order: spend amount_usd to receive shares.

        Walks the real order book ASK side level-by-level.
        """
        account = self._require_account()

        if amount_usd < MIN_ORDER_USD:
            raise OrderRejectedError(
                f"Minimum order size is ${MIN_ORDER_USD:.2f}"
            )

        # Fetch market and validate outcome against actual market outcomes
        market = self.api.get_market(slug_or_id)
        outcome = self._validate_outcome(outcome, market)

        # Fetch live order book and fee rate
        token_id = market.get_token_id(outcome)
        book = self.api.get_order_book(token_id)
        fee_rate_bps = self.api.get_fee_rate(token_id)

        if market.closed:
            raise MarketClosedError(market.slug)

        # Simulate fill against the real order book
        fill = simulate_buy_fill(book, amount_usd, fee_rate_bps, order_type)

        if not fill.filled and not fill.is_partial:
            raise OrderRejectedError(
                "Insufficient liquidity in order book (FOK rejected)"
            )

        # Check cash: need total_cost + fee
        total_outflow = fill.total_cost + fill.fee
        if total_outflow > account.cash:
            raise InsufficientBalanceError(
                required=total_outflow, available=account.cash
            )

        # Update cash
        new_cash = account.cash - total_outflow
        self.db.update_cash(new_cash)

        # Record trade
        trade = self.db.insert_trade(
            market_condition_id=market.condition_id,
            market_slug=market.slug,
            market_question=market.question,
            outcome=outcome,
            side="buy",
            order_type=order_type,
            avg_price=fill.avg_price,
            amount_usd=fill.total_cost,
            shares=fill.total_shares,
            fee_rate_bps=fee_rate_bps,
            fee=fill.fee,
            slippage=fill.slippage_bps,
            levels_filled=fill.levels_filled,
            is_partial=fill.is_partial,
        )

        # Update position
        self._update_position_after_buy(
            market=market,
            outcome=outcome,
            new_shares=fill.total_shares,
            cost=fill.total_cost + fill.fee,
            avg_fill_price=fill.avg_price,
        )

        mid = self._book_mid(book)
        self._record_equity(
            (market.condition_id, outcome, mid if mid else fill.avg_price)
        )

        updated_account = self.get_account()
        return TradeResult(trade=trade, account=updated_account)

    def _update_position_after_buy(
        self,
        *,
        market,
        outcome: str,
        new_shares: float,
        cost: float,
        avg_fill_price: float,
    ) -> None:
        """Update or create position after a buy."""
        existing = self.db.get_position(market.condition_id, outcome)
        if existing and existing.shares > 0:
            total_shares = existing.shares + new_shares
            total_cost = existing.total_cost + cost
            avg_entry = total_cost / total_shares if total_shares > 0 else 0.0
        else:
            total_shares = new_shares
            total_cost = cost
            avg_entry = avg_fill_price

        self.db.upsert_position(
            market_condition_id=market.condition_id,
            market_slug=market.slug,
            market_question=market.question,
            outcome=outcome,
            shares=total_shares,
            avg_entry_price=avg_entry,
            total_cost=total_cost,
            realized_pnl=existing.realized_pnl if existing else 0.0,
        )

    # ------------------------------------------------------------------
    # SELL — sell shares, receive USD
    # ------------------------------------------------------------------

    def sell(
        self,
        slug_or_id: str,
        outcome: str,
        shares: float,
        order_type: str = "fok",
    ) -> TradeResult:
        """Execute a sell order: sell shares to receive USD.

        Walks the real order book BID side level-by-level.
        """
        account = self._require_account()

        # Fetch market and validate outcome against actual market outcomes
        market = self.api.get_market(slug_or_id)
        outcome = self._validate_outcome(outcome, market)
        position = self.db.get_position(market.condition_id, outcome)
        if position is None or position.shares <= 0:
            raise NoPositionError(market.slug, outcome)

        if shares > position.shares:
            raise OrderRejectedError(
                f"Cannot sell {shares:.4f} shares, only hold {position.shares:.4f}"
            )

        if market.closed:
            raise MarketClosedError(market.slug)

        # Fetch live book and fee rate
        token_id = market.get_token_id(outcome)
        book = self.api.get_order_book(token_id)
        fee_rate_bps = self.api.get_fee_rate(token_id)

        # Simulate fill against the real order book
        fill = simulate_sell_fill(book, shares, fee_rate_bps, order_type)

        if not fill.filled and not fill.is_partial:
            raise OrderRejectedError(
                "Insufficient liquidity in order book (FOK rejected)"
            )

        # Net proceeds = gross - fee
        net_proceeds = fill.total_cost - fill.fee

        # Update cash
        new_cash = account.cash + net_proceeds
        self.db.update_cash(new_cash)

        # Record trade
        trade = self.db.insert_trade(
            market_condition_id=market.condition_id,
            market_slug=market.slug,
            market_question=market.question,
            outcome=outcome,
            side="sell",
            order_type=order_type,
            avg_price=fill.avg_price,
            amount_usd=fill.total_cost,
            shares=fill.total_shares,
            fee_rate_bps=fee_rate_bps,
            fee=fill.fee,
            slippage=fill.slippage_bps,
            levels_filled=fill.levels_filled,
            is_partial=fill.is_partial,
        )

        # Update position
        self._update_position_after_sell(
            market=market,
            outcome=outcome,
            sold_shares=fill.total_shares,
            proceeds=net_proceeds,
        )

        mid = self._book_mid(book)
        self._record_equity(
            (market.condition_id, outcome, mid if mid else fill.avg_price)
        )

        updated_account = self.get_account()
        return TradeResult(trade=trade, account=updated_account)

    def _update_position_after_sell(
        self,
        *,
        market,
        outcome: str,
        sold_shares: float,
        proceeds: float,
    ) -> None:
        """Update position after a sell."""
        existing = self.db.get_position(market.condition_id, outcome)
        if existing is None:
            return

        remaining_shares = existing.shares - sold_shares
        # Cost basis of sold portion
        cost_of_sold = (
            existing.avg_entry_price * sold_shares
            if existing.shares > 0
            else 0.0
        )
        realized_pnl = existing.realized_pnl + (proceeds - cost_of_sold)
        remaining_cost = existing.total_cost - cost_of_sold

        self.db.upsert_position(
            market_condition_id=market.condition_id,
            market_slug=market.slug,
            market_question=market.question,
            outcome=outcome,
            shares=max(remaining_shares, 0.0),
            avg_entry_price=existing.avg_entry_price,
            total_cost=max(remaining_cost, 0.0),
            realized_pnl=realized_pnl,
        )

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------

    def get_portfolio(self) -> list[dict]:
        """Return open positions with live prices and unrealized P&L."""
        self._require_account()
        positions = self.db.get_open_positions()
        result = []
        for pos in positions:
            try:
                token_id = self._get_token_id_for_position(pos)
                live_price = self.api.get_midpoint(token_id)
            except Exception:
                live_price = 0.0

            result.append({
                "market_slug": pos.market_slug,
                "market_question": pos.market_question,
                "outcome": pos.outcome,
                "shares": pos.shares,
                "avg_entry_price": pos.avg_entry_price,
                "total_cost": pos.total_cost,
                "live_price": live_price,
                "current_value": pos.current_value(live_price),
                "unrealized_pnl": pos.unrealized_pnl(live_price),
                "percent_pnl": pos.percent_pnl(live_price),
            })
        return result

    def _get_token_id_for_position(self, pos: Position) -> str:
        """Resolve a position to its token_id for price lookups."""
        market = self.api.get_market(pos.market_slug)
        return market.get_token_id(pos.outcome)

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    def get_balance(self) -> dict:
        """Return cash, positions value, maker income, and total account value.

        Capital locked behind active maker quotes (``maker_committed_capital``)
        is reserved out of cash but still owned, so it is added back into
        ``total_value``.  Reward income and adverse bleed already flow through
        cash; they are surfaced separately here so maker P&L is legible.
        """
        account = self._require_account()
        portfolio = self.get_portfolio()
        positions_value = sum(p["current_value"] for p in portfolio)
        maker = self.get_maker_summary()
        committed = maker["committed_capital"]
        total_value = account.cash + positions_value + committed
        return {
            "cash": account.cash,
            "starting_balance": account.starting_balance,
            "positions_value": positions_value,
            "maker_committed_capital": committed,
            "maker_reward_income": maker["reward_income"],
            "maker_inventory_pnl": maker["inventory_pnl"],
            "maker_net_pnl": maker["net_maker_pnl"],
            "total_value": total_value,
            "pnl": total_value - account.starting_balance,
        }

    # ------------------------------------------------------------------
    # Equity curve (mark-to-market over time)
    # ------------------------------------------------------------------

    @staticmethod
    def _book_mid(book) -> float | None:
        """Midpoint of an order book snapshot, or None if one side is empty."""
        if not book.bids or not book.asks:
            return None
        return (max(l.price for l in book.bids)
                + min(l.price for l in book.asks)) / 2.0

    def _record_equity(self, mark: tuple[str, str, float] | None = None) -> None:
        """Append a mark-to-market equity snapshot. Best-effort, API-free.

        Open positions are valued at *mark* (condition_id, outcome, price) for
        the just-traded leg, and at cost basis (avg_entry_price) otherwise, so
        this never issues a network call from the hot trade path.  Use
        :meth:`snapshot_equity` for a fully live mark-to-market sample.
        """
        try:
            account = self.db.get_account()
            if account is None:
                return
            equity = account.cash + self._committed_maker_capital()
            for pos in self.db.get_open_positions():
                if (mark is not None
                        and pos.market_condition_id == mark[0]
                        and pos.outcome == mark[1]):
                    price = mark[2]
                else:
                    price = pos.avg_entry_price
                equity += pos.shares * price
            self.db.record_equity(equity)
        except Exception:
            pass  # never let bookkeeping break a trade

    def _committed_maker_capital(self) -> float:
        """Cash currently reserved behind active maker quotes."""
        return sum(
            q.committed_capital for q in get_active_maker_quotes(self.db.conn)
        )

    def snapshot_equity(self) -> float:
        """Record and return a fully live mark-to-market equity snapshot.

        Values every open position at its current order-book midpoint (falling
        back to cost basis if a price is unavailable).  Call periodically to
        build a credible equity curve for Sharpe / drawdown analytics.
        """
        account = self._require_account()
        equity = account.cash + self._committed_maker_capital()
        for pos in self.db.get_open_positions():
            try:
                token_id = self._get_token_id_for_position(pos)
                price = self.api.get_midpoint(token_id)
            except Exception:
                price = 0.0
            if not price or price <= 0:
                price = pos.avg_entry_price
            equity += pos.shares * price
        self.db.record_equity(equity)
        return equity

    # ------------------------------------------------------------------
    # Trade history
    # ------------------------------------------------------------------

    def get_history(self, limit: int = 50) -> list[Trade]:
        """Return recent trades."""
        self._require_account()
        return self.db.get_trades(limit)

    # ------------------------------------------------------------------
    # Limit orders (GTC / GTD)
    # ------------------------------------------------------------------

    def place_limit_order(
        self,
        slug_or_id: str,
        outcome: str,
        side: str,
        amount: float,
        limit_price: float,
        order_type: str = "gtc",
        expires_at: str | None = None,
    ) -> dict:
        """Place a GTC or GTD limit order."""
        self._require_account()
        if side not in ("buy", "sell"):
            raise OrderRejectedError(f"Invalid side: {side!r}")
        if not (0 < limit_price < 1):
            raise OrderRejectedError(f"Limit price must be between 0 and 1, got {limit_price}")
        if order_type not in ("gtc", "gtd"):
            raise OrderRejectedError(f"Invalid order_type: {order_type!r}. Must be 'gtc' or 'gtd'.")
        if order_type == "gtd" and not expires_at:
            raise OrderRejectedError("GTD orders require expires_at timestamp")
        if side == "buy" and amount < MIN_ORDER_USD:
            raise OrderRejectedError(f"Minimum buy order size is ${MIN_ORDER_USD:.2f}, got ${amount:.2f}")

        market = self.api.get_market(slug_or_id)
        outcome = self._validate_outcome(outcome, market)
        order = create_order(
            self.db.conn,
            market_slug=market.slug,
            market_condition_id=market.condition_id,
            outcome=outcome,
            side=side,
            amount=amount,
            limit_price=limit_price,
            order_type=order_type,
            expires_at=expires_at,
        )
        return _order_to_dict(order)

    def get_pending_orders(self) -> list[dict]:
        """Return all pending limit orders."""
        orders = get_pending_orders(self.db.conn)
        return [_order_to_dict(o) for o in orders]

    def cancel_limit_order(self, order_id: int) -> dict | None:
        """Cancel a pending limit order."""
        order = cancel_order(self.db.conn, order_id)
        if order is None:
            return None
        return _order_to_dict(order)

    def cancel_all_orders(self) -> list[dict]:
        """Cancel all pending limit orders. Returns list of cancelled orders."""
        cancelled = _cancel_all_orders(self.db.conn)
        return [_order_to_dict(o) for o in cancelled]

    def check_orders(self) -> list[dict]:
        """Check all pending orders against live prices and execute fills.

        This is the agent-callable trigger. Call it periodically.
        Returns list of filled/expired orders.

        Limit price enforcement: buy orders only consume ask levels at or
        below the limit price; sell orders only consume bid levels at or
        above the limit price.  This guarantees no "price-through" fills.
        """
        self._require_account()
        results = []

        # First expire any GTD orders past their deadline
        expired = expire_orders(self.db.conn)
        for o in expired:
            results.append({"order": _order_to_dict(o), "action": "expired"})

        # Check pending orders against live order books
        pending = get_pending_orders(self.db.conn)
        for order in pending:
            try:
                market = self.api.get_market(order.market_slug)
                token_id = market.get_token_id(order.outcome)
                book = self.api.get_order_book(token_id)
                fee_rate_bps = self.api.get_fee_rate(token_id)

                if order.side == "buy":
                    # Only fill at ask levels <= limit_price
                    best_ask = min((l.price for l in book.asks), default=None)
                    if best_ask is None or best_ask > order.limit_price:
                        continue
                    fill = simulate_buy_fill(
                        book, order.amount, fee_rate_bps, "fak",
                        max_price=order.limit_price,
                    )
                else:
                    # Only fill at bid levels >= limit_price
                    best_bid = max((l.price for l in book.bids), default=None)
                    if best_bid is None or best_bid < order.limit_price:
                        continue
                    fill = simulate_sell_fill(
                        book, order.amount, fee_rate_bps, "fak",
                        min_price=order.limit_price,
                    )

                if not fill.filled and not fill.is_partial:
                    continue  # No fillable liquidity within limit

                # Execute the fill through normal trade recording
                if order.side == "buy":
                    self._execute_limit_buy(market, order, fill, fee_rate_bps)
                else:
                    self._execute_limit_sell(market, order, fill, fee_rate_bps)

                updated = mark_filled(self.db.conn, order.id)
                results.append({
                    "order": _order_to_dict(updated),
                    "action": "filled",
                })
            except _PERMANENT_ORDER_ERRORS as e:
                # Permanent failure — mark rejected so it's not retried
                updated = reject_order(self.db.conn, order.id)
                results.append({
                    "order": _order_to_dict(updated),
                    "action": "rejected",
                    "reason": str(e),
                })
            except Exception:
                continue  # Transient errors (network, API) — retry next check

        return results

    def _execute_limit_buy(self, market, order, fill, fee_rate_bps: int) -> None:
        """Record a limit buy fill using a pre-computed FillResult."""
        account = self._require_account()
        total_outflow = fill.total_cost + fill.fee
        if total_outflow > account.cash:
            raise InsufficientBalanceError(
                required=total_outflow, available=account.cash,
            )
        self.db.update_cash(account.cash - total_outflow)
        self.db.insert_trade(
            market_condition_id=market.condition_id,
            market_slug=market.slug,
            market_question=market.question,
            outcome=order.outcome,
            side="buy",
            order_type="fak",
            avg_price=fill.avg_price,
            amount_usd=fill.total_cost,
            shares=fill.total_shares,
            fee_rate_bps=fee_rate_bps,
            fee=fill.fee,
            slippage=fill.slippage_bps,
            levels_filled=fill.levels_filled,
            is_partial=fill.is_partial,
        )
        self._update_position_after_buy(
            market=market,
            outcome=order.outcome,
            new_shares=fill.total_shares,
            cost=fill.total_cost + fill.fee,
            avg_fill_price=fill.avg_price,
        )
        self._record_equity((market.condition_id, order.outcome, fill.avg_price))

    def _execute_limit_sell(self, market, order, fill, fee_rate_bps: int) -> None:
        """Record a limit sell fill using a pre-computed FillResult."""
        account = self._require_account()
        position = self.db.get_position(market.condition_id, order.outcome)
        if position is None or position.shares <= 0:
            raise NoPositionError(market.slug, order.outcome)
        if fill.total_shares > position.shares:
            raise OrderRejectedError(
                f"Cannot sell {fill.total_shares:.4f} shares, "
                f"only hold {position.shares:.4f}"
            )
        net_proceeds = fill.total_cost - fill.fee
        self.db.update_cash(account.cash + net_proceeds)
        self.db.insert_trade(
            market_condition_id=market.condition_id,
            market_slug=market.slug,
            market_question=market.question,
            outcome=order.outcome,
            side="sell",
            order_type="fak",
            avg_price=fill.avg_price,
            amount_usd=fill.total_cost,
            shares=fill.total_shares,
            fee_rate_bps=fee_rate_bps,
            fee=fill.fee,
            slippage=fill.slippage_bps,
            levels_filled=fill.levels_filled,
            is_partial=fill.is_partial,
        )
        self._update_position_after_sell(
            market=market,
            outcome=order.outcome,
            sold_shares=fill.total_shares,
            proceeds=net_proceeds,
        )
        self._record_equity((market.condition_id, order.outcome, fill.avg_price))

    def watch_prices(
        self, slugs_or_ids: list[str], outcomes: list[str] | None = None,
    ) -> list[dict]:
        """Fetch live midpoint prices for given markets.

        Agent calls this to monitor prices before deciding to trade.
        """
        results = []
        if outcomes is None:
            outcomes = ["yes"]
        for slug in slugs_or_ids:
            try:
                market = self.api.get_market(slug)
            except Exception:
                continue  # Market not found or API error
            for outcome in outcomes:
                outcome = outcome.lower()
                token_id = market.get_token_id(outcome)  # raises ValueError for invalid
                try:
                    mid = self.api.get_midpoint(token_id)
                except Exception:
                    continue  # API error fetching price
                results.append({
                    "market_slug": market.slug,
                    "outcome": outcome,
                    "midpoint": mid,
                    "condition_id": market.condition_id,
                })
        return results

    # ------------------------------------------------------------------
    # Maker quotes (liquidity-rewards two-sided quoting)
    # ------------------------------------------------------------------

    def place_maker_quote(
        self,
        slug_or_id: str,
        outcome: str = "yes",
        *,
        size: float | None = None,
        half_spread_cents: float | None = None,
        cancel_efficiency: float = 0.0,
        max_inventory: float | None = None,
        skew_strength: float = MAKER_SKEW_STRENGTH,
        now: datetime | None = None,
    ) -> dict:
        """Place a resting two-sided maker quote to earn liquidity rewards.

        The quote rests ``half_spread_cents`` either side of the (inventory-skewed)
        mid with ``size`` shares per side (defaulting to the pool's ``min_size``
        and one tick in-band).  Pool config is pulled live from the CLOB; the
        market must be in the liquidity-rewards program.  Reserves
        ``committed_capital`` out of cash until cancelled.

        Inventory management (verified essential against trends):
        - ``cancel_efficiency`` (0-1): colocation lever — fraction of adverse
          fills a fast canceller pulls before they trade (scales fill size down).
        - ``max_inventory`` (shares): position cap; at the cap the quote goes
          one-sided (only the flattening side fills).  Defaults to 4 × size.
        - ``skew_strength`` (≥0): inventory-skew lean — shifts the quote centre
          away from inventory so the book mean-reverts toward flat (A-S idea).
        A persistent trend still bleeds the held inventory (skew/cap bound it,
        the drift-exit in ``accrue_maker_rewards`` ultimately flattens + exits);
        the durable defence is selecting calm, range-bound pools.
        """
        account = self._require_account()
        market = self.api.get_market(slug_or_id)
        outcome = self._validate_outcome(outcome, market)
        if market.closed:
            raise MarketClosedError(market.slug)

        pool = self.api.get_reward_config(market.condition_id)
        if pool is None:
            raise OrderRejectedError(
                f"{market.slug} is not in the liquidity-rewards program"
            )
        max_spread_c = pool["max_spread"]
        min_size = pool["min_size"]
        daily_rate = pool["daily"]
        tick = pool["tick"]

        size = min_size if size is None else size
        if size < min_size:
            raise OrderRejectedError(
                f"Maker size {size:.2f} below pool min_size {min_size:.2f}"
            )
        half_spread_c = (tick * 100.0) if half_spread_cents is None else half_spread_cents
        if half_spread_c <= 0 or half_spread_c > max_spread_c:
            raise OrderRejectedError(
                f"half_spread_cents must be in (0, {max_spread_c}], got {half_spread_c}"
            )
        if not 0.0 <= cancel_efficiency <= 1.0:
            raise OrderRejectedError(
                f"cancel_efficiency must be in [0, 1], got {cancel_efficiency}"
            )
        if skew_strength < 0.0:
            raise OrderRejectedError(
                f"skew_strength must be >= 0, got {skew_strength}"
            )
        cap_shares = (MAKER_CAP_MULT * size) if max_inventory is None else max_inventory
        if cap_shares <= 0:
            raise OrderRejectedError(
                f"max_inventory must be > 0, got {cap_shares}"
            )

        token_id = market.get_token_id(outcome)
        mid = self.api.get_midpoint(token_id)
        if not (0.0 < mid < 1.0):
            raise OrderRejectedError("No valid midpoint to anchor the maker quote")

        cap = committed_capital(size, half_spread_c)
        if cap > account.cash:
            raise InsufficientBalanceError(required=cap, available=account.cash)
        self.db.update_cash(account.cash - cap)

        quote = create_maker_quote(
            self.db.conn,
            market_slug=market.slug,
            market_condition_id=market.condition_id,
            outcome=outcome,
            token_id=token_id,
            size=size,
            half_spread_c=half_spread_c,
            max_spread_c=max_spread_c,
            min_size=min_size,
            daily_rate=daily_rate,
            tick=tick,
            cancel_efficiency=cancel_efficiency,
            max_inventory=cap_shares,
            skew_strength=skew_strength,
            entry_mid=mid,
            committed_capital=cap,
            last_mid=mid,
            last_accrued_at=_utcnow(now).isoformat(),
        )
        self._record_equity()
        return _maker_quote_to_dict(quote)

    def get_maker_quotes(self) -> list[dict]:
        """Return all active maker quotes."""
        self._require_account()
        return [_maker_quote_to_dict(q) for q in get_active_maker_quotes(self.db.conn)]

    def cancel_maker_quote(self, quote_id: int) -> dict | None:
        """Cancel an active maker quote and release its reserved capital."""
        self._require_account()
        quote = get_maker_quote(self.db.conn, quote_id)
        if quote is None or quote.status != "active":
            return None
        account = self.get_account()
        self.db.update_cash(account.cash + quote.committed_capital)
        updated = _cancel_maker_quote(self.db.conn, quote_id)
        self._record_equity()
        return _maker_quote_to_dict(updated)

    def accrue_maker_rewards(self, now: datetime | None = None) -> list[dict]:
        """Advance every active maker quote one inventory-aware poll.

        The agent-callable poll (call it periodically, like ``check_orders``).
        For each active quote:
          1. RECONCILE against the live program — if the pool stopped paying
             (rewards ended / market resolved) flatten, free capital, cancel.
          2. Accrue the reward share of the pool's CURRENT daily rate over the
             elapsed in-band time.
          3. INVENTORY: mark the held inventory at the new mid (the trend P&L),
             then fill the resting inventory-skewed quote as the mid moved —
             accumulating inventory (capped at ``max_inventory``, scaled by
             ``cancel_efficiency``) and booking the adverse pick-off cost.
          4. DRIFT-EXIT: if the mid has drifted a full band from ``entry_mid``
             (a persistent trend or jump), flatten the inventory at mid, free
             capital and cancel — the regime is no longer the calm pool we
             entered.  Net cash per poll = reward + held-MTM − pick-off cost.
        """
        self._require_account()
        now_dt = _utcnow(now)
        results: list[dict] = []
        for quote in get_active_maker_quotes(self.db.conn):
            try:
                pool = self.api.get_reward_config(quote.market_condition_id)
            except Exception:
                continue  # transient API/network error — retry next poll
            daily_rate = pool["daily"] if pool else 0.0
            if pool is None or daily_rate <= 0:
                # rewards ended / market resolved → flatten, free capital, stop
                self._exit_maker_quote(quote, quote.last_mid, "rewards_ended", results)
                continue
            try:
                book = self.api.get_order_book(quote.token_id)
                mid = self.api.get_midpoint(quote.token_id)
            except Exception:
                continue  # transient API/network error — retry next poll
            if not (0.0 < mid < 1.0):
                continue

            last_dt = datetime.fromisoformat(quote.last_accrued_at)
            seconds = max(0.0, (now_dt - last_dt).total_seconds())

            # 2. Reward over the elapsed in-band time.
            existing_qmin = book_inband_qmin(book, mid, quote.max_spread_c)
            share = maker_reward_share(
                quote.size, quote.half_spread_c, quote.max_spread_c, existing_qmin
            )
            reward = reward_accrual(share, daily_rate, seconds)

            # 3. Inventory: held mark-to-market + the new fill from the move.
            cap = (quote.max_inventory if quote.max_inventory > 0
                   else MAKER_CAP_MULT * quote.size)  # 0 = unset → default cap
            held_mtm = quote.inventory * (mid - quote.last_mid)
            d_inv, fill_loss = maker_fill(
                quote.last_mid, mid, quote.inventory, quote.size,
                quote.half_spread_c, quote.skew_strength, quote.cancel_efficiency,
                cap,
            )
            new_inventory = quote.inventory + d_inv
            inv_pnl_delta = held_mtm - fill_loss

            # 4. Drift-exit: a full-band move from entry → flatten + cancel.
            entry_mid = quote.entry_mid if quote.entry_mid > 0 else mid
            if abs(mid - entry_mid) >= quote.max_spread_c / 100.0:
                # held inventory already marked at mid via inv_pnl_delta; bank the
                # reward + inventory delta, then flatten (inventory → 0) and exit.
                self._credit_maker(reward + inv_pnl_delta)
                final = update_maker_quote_accrual(
                    self.db.conn, quote.id,
                    accrued_rewards=quote.accrued_rewards + reward,
                    realized_bleed=quote.realized_bleed + fill_loss,
                    fills=quote.fills + (1 if d_inv != 0 else 0),
                    last_mid=mid,
                    last_accrued_at=now_dt.isoformat(),
                    inventory=0.0,
                    inventory_pnl=quote.inventory_pnl + inv_pnl_delta,
                )
                self._exit_maker_quote(final, mid, "drift_exit", results,
                                       reward=reward, fill_loss=fill_loss,
                                       inventory_pnl_delta=inv_pnl_delta,
                                       share=share, seconds=seconds)
                continue

            self._credit_maker(reward + inv_pnl_delta)
            updated = update_maker_quote_accrual(
                self.db.conn,
                quote.id,
                accrued_rewards=quote.accrued_rewards + reward,
                realized_bleed=quote.realized_bleed + fill_loss,
                fills=quote.fills + (1 if d_inv != 0 else 0),
                last_mid=mid,
                last_accrued_at=now_dt.isoformat(),
                inventory=new_inventory,
                inventory_pnl=quote.inventory_pnl + inv_pnl_delta,
            )
            results.append({
                "quote": _maker_quote_to_dict(updated),
                "reward": round(reward, 6),
                "fill_loss": round(fill_loss, 6),
                "inventory_pnl_delta": round(inv_pnl_delta, 6),
                "inventory": round(new_inventory, 4),
                "share": round(share, 6),
                "seconds": round(seconds, 2),
                "mid": mid,
            })
        self._record_equity()
        return results

    def _credit_maker(self, amount: float) -> None:
        """Mark a maker cash delta (reward + inventory P&L) to the account."""
        self.db.update_cash(self.get_account().cash + amount)

    def _exit_maker_quote(
        self, quote, mid: float, reason: str, results: list[dict], **extra,
    ) -> None:
        """Flatten a quote's inventory at *mid*, free its capital, and cancel it.

        ``quote.inventory_pnl`` is already marked at *mid*, so flattening realises
        it at no extra cost; we just release the reserved capital and book a
        crossing cost would go here for a true taker exit (paper: flatten at mid).
        """
        self.db.update_cash(self.get_account().cash + quote.committed_capital)
        cancelled = _cancel_maker_quote(self.db.conn, quote.id)
        row = {"quote": _maker_quote_to_dict(cancelled), "reconciled": reason,
               "mid": mid}
        row.update({k: round(v, 6) for k, v in extra.items()})
        results.append(row)

    def suggest_maker_half_spread(
        self,
        slug_or_id: str,
        outcome: str = "yes",
        *,
        cancel_efficiency: float = 0.0,
        poll_seconds: float = 60.0,
    ) -> dict:
        """Recommend the net-optimal half-spread (cents) for a reward pool.

        Pulls the pool config, live book (competition Qmin) and recent price
        history (volatility), then grid-searches the offset that maximises
        ``reward − adverse_bleed`` per day at the given ``poll_seconds`` re-quote
        cadence and ``cancel_efficiency``.  Embeds the Avellaneda-Stoikov tension
        adapted to PM's reward subsidy: tighter earns a quadratically larger
        reward share but bleeds more to adverse selection (see ``optimal_half_spread``).
        """
        self._require_account()
        market = self.api.get_market(slug_or_id)
        outcome = self._validate_outcome(outcome, market)
        if not 0.0 <= cancel_efficiency <= 1.0:
            raise OrderRejectedError(
                f"cancel_efficiency must be in [0, 1], got {cancel_efficiency}"
            )
        if poll_seconds <= 0:
            raise OrderRejectedError(f"poll_seconds must be > 0, got {poll_seconds}")

        pool = self.api.get_reward_config(market.condition_id)
        if pool is None:
            raise OrderRejectedError(
                f"{market.slug} is not in the liquidity-rewards program"
            )
        token_id = market.get_token_id(outcome)
        book = self.api.get_order_book(token_id)
        mid = self.api.get_midpoint(token_id)
        if not (0.0 < mid < 1.0):
            raise OrderRejectedError("No valid midpoint to anchor the maker quote")

        existing_qmin = book_inband_qmin(book, mid, pool["max_spread"])
        try:
            history = self.api.prices_history(token_id)
        except Exception:
            history = []
        sigma_c = realized_sigma_c_from_history(history, poll_seconds)

        rec = optimal_half_spread(
            daily_rate=pool["daily"],
            max_spread_c=pool["max_spread"],
            min_size=pool["min_size"],
            tick_c=pool["tick"] * 100.0,
            existing_qmin=existing_qmin,
            sigma_c=sigma_c,
            periods_per_day=86_400.0 / poll_seconds,
            cancel_efficiency=cancel_efficiency,
        )
        rec.update({
            "market_slug": market.slug,
            "condition_id": market.condition_id,
            "outcome": outcome,
            "mid": mid,
            "sigma_c": round(sigma_c, 4),
            "existing_qmin": round(existing_qmin, 4),
            "max_spread_c": pool["max_spread"],
            "min_size": pool["min_size"],
            "daily_rate": pool["daily"],
            "tick_c": round(pool["tick"] * 100.0, 4),
            "poll_seconds": poll_seconds,
            "cancel_efficiency": cancel_efficiency,
        })
        return rec

    def get_maker_summary(self) -> dict:
        """Aggregate maker P&L: reward income, inventory P&L, pick-off bleed, net.

        ``net_maker_pnl = reward_income + inventory_pnl`` (inventory P&L = held
        mark-to-market + the adverse pick-off cost, usually ≤ 0 in a trend).
        ``adverse_bleed`` is the pick-off sub-component, reported for colour.
        """
        self._require_account()
        quotes = get_all_maker_quotes(self.db.conn)
        committed = sum(q.committed_capital for q in quotes if q.status == "active")
        reward_income = sum(q.accrued_rewards for q in quotes)
        inventory_pnl = sum(q.inventory_pnl for q in quotes)
        bleed = sum(q.realized_bleed for q in quotes)
        open_inventory = sum(q.inventory for q in quotes if q.status == "active")
        return {
            "active_quotes": sum(1 for q in quotes if q.status == "active"),
            "total_quotes": len(quotes),
            "committed_capital": committed,
            "open_inventory": open_inventory,
            "reward_income": reward_income,
            "inventory_pnl": inventory_pnl,
            "adverse_bleed": bleed,
            "net_maker_pnl": reward_income + inventory_pnl,
        }

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve_market(self, slug_or_id: str) -> list[ResolveResult]:
        """Resolve a market's positions, paying out $1/share for winner."""
        account = self._require_account()
        market = self.api.get_market(slug_or_id)

        if not market.closed:
            raise MarketClosedError(
                f"{market.slug} is not yet closed/resolved"
            )

        positions = self.db.get_positions_for_market(market.condition_id)
        if not positions:
            raise NoPositionError(market.slug, "any")

        winning_outcome = _determine_winner(market)

        results = []
        for pos in positions:
            if pos.is_resolved or pos.shares <= 0:
                continue

            if pos.outcome == winning_outcome:
                payout = pos.shares * 1.0
            else:
                payout = 0.0

            resolved_pos = self.db.resolve_position(
                market.condition_id, pos.outcome, payout
            )

            # Add payout to cash
            account = self.get_account()
            new_cash = account.cash + payout
            self.db.update_cash(new_cash)
            account = self.get_account()

            results.append(ResolveResult(
                position=resolved_pos,
                payout=payout,
                account=account,
            ))

        self._record_equity()
        return results

    def resolve_all(self) -> list[ResolveResult]:
        """Resolve all open positions in closed markets.

        Skips markets that fail due to transient API/network errors.
        Raises on permanent resolution failures (e.g. ambiguous outcomes).
        """
        self._require_account()
        positions = self.db.get_open_positions()
        all_results = []

        seen_markets: set[str] = set()
        for pos in positions:
            if pos.market_condition_id in seen_markets:
                continue
            try:
                market = self.api.get_market(pos.market_slug)
                if market.closed:
                    seen_markets.add(pos.market_condition_id)
                    results = self.resolve_market(pos.market_slug)
                    all_results.extend(results)
            except (ApiError, ConnectionError, TimeoutError, OSError):
                continue  # Transient — retry on next call

        return all_results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _determine_winner(market) -> str:
    """Determine the winning outcome from a resolved market's prices.

    Raises SimError if no outcome has price >= 0.99, preventing silent
    zero-payout on ambiguous or partially-resolved markets.
    """
    for i, outcome in enumerate(market.outcomes):
        price = market.outcome_prices[i] if i < len(market.outcome_prices) else 0.0
        if price >= 0.99:
            return outcome.lower()
    prices = dict(zip(market.outcomes, market.outcome_prices))
    raise AmbiguousResolutionError(market.slug, prices)


def _order_to_dict(order) -> dict:
    """Convert a LimitOrder to a JSON-safe dict."""
    return {
        "id": order.id,
        "market_slug": order.market_slug,
        "market_condition_id": order.market_condition_id,
        "outcome": order.outcome,
        "side": order.side,
        "amount": order.amount,
        "limit_price": order.limit_price,
        "order_type": order.order_type,
        "expires_at": order.expires_at,
        "status": order.status,
        "created_at": order.created_at,
        "filled_at": order.filled_at,
    }


def _utcnow(now: datetime | None) -> datetime:
    """Return *now* if provided, else the current UTC time (for testability)."""
    return now if now is not None else datetime.now(timezone.utc)


def _maker_quote_to_dict(quote) -> dict:
    """Convert a MakerQuote to a JSON-safe dict."""
    return {
        "id": quote.id,
        "market_slug": quote.market_slug,
        "market_condition_id": quote.market_condition_id,
        "outcome": quote.outcome,
        "token_id": quote.token_id,
        "size": quote.size,
        "half_spread_c": quote.half_spread_c,
        "max_spread_c": quote.max_spread_c,
        "min_size": quote.min_size,
        "daily_rate": quote.daily_rate,
        "tick": quote.tick,
        "cancel_efficiency": quote.cancel_efficiency,
        "max_inventory": quote.max_inventory,
        "skew_strength": quote.skew_strength,
        "inventory": quote.inventory,
        "inventory_pnl": quote.inventory_pnl,
        "entry_mid": quote.entry_mid,
        "committed_capital": quote.committed_capital,
        "accrued_rewards": quote.accrued_rewards,
        "realized_bleed": quote.realized_bleed,
        "net_pnl": quote.accrued_rewards + quote.inventory_pnl,
        "fills": quote.fills,
        "status": quote.status,
        "last_mid": quote.last_mid,
        "created_at": quote.created_at,
        "last_accrued_at": quote.last_accrued_at,
    }
