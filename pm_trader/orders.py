"""Limit order management for pm-trader.

GTC (Good-Til-Cancelled): rests until price target is hit or manually cancelled.
GTD (Good-Til-Date): GTC with an expiry timestamp.

Orders are stored in SQLite and checked against live midpoint prices
when the agent calls `pm-trader orders check`.

This module also owns ``maker_quotes`` — resting two-sided liquidity-rewards
quotes.  Unlike a one-sided limit order (which earns no reward, since the
program scores the binding/lighter side and an absent side is 0), a maker quote
rests ``half_spread_c`` cents either side of mid and accrues a share of the
pool's daily USDC rate over its in-band uptime, net of adverse-selection bleed
when the mid jumps through it.  Storage only — the accrual math lives in
``orderbook`` and the orchestration in ``engine``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone


def _normalize_timestamp(ts: str) -> str:
    """Normalize an ISO timestamp to a consistent format for TEXT comparison.

    Replaces 'Z' suffix with '+00:00' and ensures the string sorts correctly
    as TEXT in SQLite.
    """
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return ts


@dataclass
class LimitOrder:
    """A pending limit order."""

    id: int
    market_slug: str
    market_condition_id: str
    outcome: str
    side: str  # "buy" or "sell"
    amount: float  # USD for buy, shares for sell
    limit_price: float
    order_type: str  # "gtc" or "gtd"
    expires_at: str | None  # ISO timestamp for GTD, None for GTC
    status: str  # "pending", "filled", "cancelled", "expired"
    created_at: str
    filled_at: str | None = None


ORDERS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS limit_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug TEXT NOT NULL,
    market_condition_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (length(outcome) > 0),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    amount REAL NOT NULL,
    limit_price REAL NOT NULL,
    order_type TEXT NOT NULL CHECK (order_type IN ('gtc', 'gtd')),
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'filled', 'cancelled', 'expired', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    filled_at TEXT
);

CREATE TABLE IF NOT EXISTS maker_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug TEXT NOT NULL,
    market_condition_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (length(outcome) > 0),
    token_id TEXT NOT NULL,
    size REAL NOT NULL,
    half_spread_c REAL NOT NULL,
    max_spread_c REAL NOT NULL,
    min_size REAL NOT NULL,
    daily_rate REAL NOT NULL,
    tick REAL NOT NULL,
    cancel_efficiency REAL NOT NULL DEFAULT 0,
    max_inventory REAL NOT NULL DEFAULT 0,
    skew_strength REAL NOT NULL DEFAULT 0,
    inventory REAL NOT NULL DEFAULT 0,
    inventory_pnl REAL NOT NULL DEFAULT 0,
    entry_mid REAL NOT NULL DEFAULT 0,
    committed_capital REAL NOT NULL,
    accrued_rewards REAL NOT NULL DEFAULT 0,
    realized_bleed REAL NOT NULL DEFAULT 0,
    fills INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    last_mid REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_accrued_at TEXT NOT NULL
);
"""


def init_orders_schema(conn: sqlite3.Connection) -> None:
    """Create the limit_orders and maker_quotes tables if they don't exist."""
    conn.executescript(ORDERS_SCHEMA)
    _migrate_maker_quotes(conn)


_MAKER_ADDED_COLUMNS = (
    # column, DDL — added after the table first shipped; backfilled to keep
    # databases created by an earlier schema readable (CREATE TABLE IF NOT
    # EXISTS won't alter them).  All default to 0: cancel_efficiency 0 = the
    # conservative always-picked-off case; max_inventory/skew_strength 0 = the
    # legacy mark-to-market path (inventory mode off).
    ("cancel_efficiency", "REAL NOT NULL DEFAULT 0"),
    ("max_inventory", "REAL NOT NULL DEFAULT 0"),
    ("skew_strength", "REAL NOT NULL DEFAULT 0"),
    ("inventory", "REAL NOT NULL DEFAULT 0"),
    ("inventory_pnl", "REAL NOT NULL DEFAULT 0"),
    ("entry_mid", "REAL NOT NULL DEFAULT 0"),
)


def _migrate_maker_quotes(conn: sqlite3.Connection) -> None:
    """Additively migrate an existing maker_quotes table to the current schema."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(maker_quotes)").fetchall()}
    added = set()
    for name, ddl in _MAKER_ADDED_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE maker_quotes ADD COLUMN {name} {ddl}")
            added.add(name)
    if "entry_mid" in added:
        # anchor pre-existing quotes at their last known mid so the drift-exit
        # measures from a real reference (not the 0 default).
        conn.execute("UPDATE maker_quotes SET entry_mid = last_mid")
    if "inventory_pnl" in added:
        # the old mark-to-market model's realized_bleed WAS the (negative)
        # inventory P&L; carry it over so net_maker_pnl = reward + inventory_pnl
        # stays continuous across the model change.
        conn.execute("UPDATE maker_quotes SET inventory_pnl = -realized_bleed")
    if added:
        conn.commit()


def create_order(
    conn: sqlite3.Connection,
    *,
    market_slug: str,
    market_condition_id: str,
    outcome: str,
    side: str,
    amount: float,
    limit_price: float,
    order_type: str = "gtc",
    expires_at: str | None = None,
) -> LimitOrder:
    """Create a new pending limit order."""
    normalized_expires = _normalize_timestamp(expires_at) if expires_at else None
    cursor = conn.execute(
        """\
        INSERT INTO limit_orders (
            market_slug, market_condition_id, outcome, side,
            amount, limit_price, order_type, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (market_slug, market_condition_id, outcome, side,
         amount, limit_price, order_type, normalized_expires),
    )
    conn.commit()
    return _get_order(conn, cursor.lastrowid)


def get_pending_orders(conn: sqlite3.Connection) -> list[LimitOrder]:
    """Return all pending limit orders."""
    rows = conn.execute(
        "SELECT * FROM limit_orders WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    return [_row_to_order(r) for r in rows]


def get_order(conn: sqlite3.Connection, order_id: int) -> LimitOrder | None:
    """Return a specific order, or None."""
    return _get_order(conn, order_id)


def cancel_order(conn: sqlite3.Connection, order_id: int) -> LimitOrder | None:
    """Cancel a pending order. Returns the updated order or None if not found."""
    order = _get_order(conn, order_id)
    if order is None or order.status != "pending":
        return None
    conn.execute(
        "UPDATE limit_orders SET status = 'cancelled' WHERE id = ?",
        (order_id,),
    )
    conn.commit()
    return _get_order(conn, order_id)


def cancel_all_orders(conn: sqlite3.Connection) -> list[LimitOrder]:
    """Cancel all pending orders. Returns list of cancelled orders."""
    pending = get_pending_orders(conn)
    if not pending:
        return []
    conn.execute(
        "UPDATE limit_orders SET status = 'cancelled' WHERE status = 'pending'"
    )
    conn.commit()
    return [replace(o, status="cancelled") for o in pending]


def mark_filled(conn: sqlite3.Connection, order_id: int) -> LimitOrder:
    """Mark an order as filled."""
    conn.execute(
        "UPDATE limit_orders SET status = 'filled', filled_at = datetime('now') WHERE id = ?",
        (order_id,),
    )
    conn.commit()
    return _get_order(conn, order_id)


def reject_order(conn: sqlite3.Connection, order_id: int) -> LimitOrder:
    """Mark an order as permanently rejected (unfillable)."""
    conn.execute(
        "UPDATE limit_orders SET status = 'rejected' WHERE id = ?",
        (order_id,),
    )
    conn.commit()
    return _get_order(conn, order_id)


def expire_orders(conn: sqlite3.Connection) -> list[LimitOrder]:
    """Expire all GTD orders past their expires_at. Returns expired orders."""
    now = _normalize_timestamp(datetime.now(timezone.utc).isoformat())
    rows = conn.execute(
        """\
        SELECT * FROM limit_orders
        WHERE status = 'pending' AND order_type = 'gtd' AND expires_at <= ?
        """,
        (now,),
    ).fetchall()

    if rows:
        conn.execute(
            """\
            UPDATE limit_orders SET status = 'expired'
            WHERE status = 'pending' AND order_type = 'gtd' AND expires_at <= ?
            """,
            (now,),
        )
        conn.commit()

    return [_row_to_order(r) for r in rows]


def should_fill(order: LimitOrder, best_price: float) -> bool:
    """Check if a limit order should be filled at the given best price.

    Buy limit: fill when best_ask <= limit_price (can buy at or below target)
    Sell limit: fill when best_bid >= limit_price (can sell at or above target)
    """
    if order.side == "buy":
        return best_price <= order.limit_price
    else:  # sell
        return best_price >= order.limit_price


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_order(conn: sqlite3.Connection, order_id: int) -> LimitOrder | None:
    row = conn.execute(
        "SELECT * FROM limit_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_order(row)


def _row_to_order(row: sqlite3.Row) -> LimitOrder:
    return LimitOrder(
        id=row["id"],
        market_slug=row["market_slug"],
        market_condition_id=row["market_condition_id"],
        outcome=row["outcome"],
        side=row["side"],
        amount=row["amount"],
        limit_price=row["limit_price"],
        order_type=row["order_type"],
        expires_at=row["expires_at"],
        status=row["status"],
        created_at=row["created_at"],
        filled_at=row["filled_at"],
    )


# ---------------------------------------------------------------------------
# Maker quotes (two-sided liquidity-rewards quotes)
# ---------------------------------------------------------------------------


@dataclass
class MakerQuote:
    """A resting two-sided maker quote that accrues liquidity rewards.

    The quote rests ``half_spread_c`` cents either side of the midpoint with
    ``size`` shares per side.  Pool config (``max_spread_c``, ``min_size``,
    ``daily_rate``, ``tick``) is captured at placement.  ``accrued_rewards`` and
    ``realized_bleed`` are running totals; ``committed_capital`` is the cash
    reserved while the quote is active.
    """

    id: int
    market_slug: str
    market_condition_id: str
    outcome: str
    token_id: str
    size: float
    half_spread_c: float
    max_spread_c: float
    min_size: float
    daily_rate: float
    tick: float
    cancel_efficiency: float
    max_inventory: float
    skew_strength: float
    inventory: float
    inventory_pnl: float
    entry_mid: float
    committed_capital: float
    accrued_rewards: float
    realized_bleed: float
    fills: int
    status: str  # "active" or "cancelled"
    last_mid: float
    created_at: str
    last_accrued_at: str


def create_maker_quote(
    conn: sqlite3.Connection,
    *,
    market_slug: str,
    market_condition_id: str,
    outcome: str,
    token_id: str,
    size: float,
    half_spread_c: float,
    max_spread_c: float,
    min_size: float,
    daily_rate: float,
    tick: float,
    cancel_efficiency: float,
    committed_capital: float,
    last_mid: float,
    last_accrued_at: str,
    max_inventory: float = 0.0,
    skew_strength: float = 0.0,
    entry_mid: float = 0.0,
) -> MakerQuote:
    """Create a new active maker quote and return it."""
    cursor = conn.execute(
        """\
        INSERT INTO maker_quotes (
            market_slug, market_condition_id, outcome, token_id,
            size, half_spread_c, max_spread_c, min_size, daily_rate, tick,
            cancel_efficiency, max_inventory, skew_strength, entry_mid,
            committed_capital, last_mid, last_accrued_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_slug, market_condition_id, outcome, token_id,
            size, half_spread_c, max_spread_c, min_size, daily_rate, tick,
            cancel_efficiency, max_inventory, skew_strength, entry_mid,
            committed_capital, last_mid, last_accrued_at,
        ),
    )
    conn.commit()
    return _get_maker_quote(conn, cursor.lastrowid)


def get_maker_quote(conn: sqlite3.Connection, quote_id: int) -> MakerQuote | None:
    """Return a specific maker quote, or None."""
    return _get_maker_quote(conn, quote_id)


def get_active_maker_quotes(conn: sqlite3.Connection) -> list[MakerQuote]:
    """Return all active (resting) maker quotes."""
    rows = conn.execute(
        "SELECT * FROM maker_quotes WHERE status = 'active' ORDER BY id"
    ).fetchall()
    return [_row_to_maker_quote(r) for r in rows]


def get_all_maker_quotes(conn: sqlite3.Connection) -> list[MakerQuote]:
    """Return all maker quotes (active and cancelled), for P&L accounting."""
    rows = conn.execute("SELECT * FROM maker_quotes ORDER BY id").fetchall()
    return [_row_to_maker_quote(r) for r in rows]


def cancel_maker_quote(conn: sqlite3.Connection, quote_id: int) -> MakerQuote | None:
    """Cancel an active maker quote. Returns the updated quote or None."""
    quote = _get_maker_quote(conn, quote_id)
    if quote is None or quote.status != "active":
        return None
    conn.execute(
        "UPDATE maker_quotes SET status = 'cancelled' WHERE id = ?",
        (quote_id,),
    )
    conn.commit()
    return _get_maker_quote(conn, quote_id)


def update_maker_quote_accrual(
    conn: sqlite3.Connection,
    quote_id: int,
    *,
    accrued_rewards: float,
    realized_bleed: float,
    fills: int,
    last_mid: float,
    last_accrued_at: str,
    inventory: float = 0.0,
    inventory_pnl: float = 0.0,
) -> MakerQuote:
    """Persist an accrual step's updated totals, inventory, and timestamps."""
    conn.execute(
        """\
        UPDATE maker_quotes SET
            accrued_rewards = ?,
            realized_bleed = ?,
            fills = ?,
            last_mid = ?,
            last_accrued_at = ?,
            inventory = ?,
            inventory_pnl = ?
        WHERE id = ?
        """,
        (accrued_rewards, realized_bleed, fills, last_mid, last_accrued_at,
         inventory, inventory_pnl, quote_id),
    )
    conn.commit()
    return _get_maker_quote(conn, quote_id)


def _get_maker_quote(conn: sqlite3.Connection, quote_id: int) -> MakerQuote | None:
    row = conn.execute(
        "SELECT * FROM maker_quotes WHERE id = ?", (quote_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_maker_quote(row)


def _row_to_maker_quote(row: sqlite3.Row) -> MakerQuote:
    return MakerQuote(
        id=row["id"],
        market_slug=row["market_slug"],
        market_condition_id=row["market_condition_id"],
        outcome=row["outcome"],
        token_id=row["token_id"],
        size=row["size"],
        half_spread_c=row["half_spread_c"],
        max_spread_c=row["max_spread_c"],
        min_size=row["min_size"],
        daily_rate=row["daily_rate"],
        tick=row["tick"],
        cancel_efficiency=row["cancel_efficiency"],
        max_inventory=row["max_inventory"],
        skew_strength=row["skew_strength"],
        inventory=row["inventory"],
        inventory_pnl=row["inventory_pnl"],
        entry_mid=row["entry_mid"],
        committed_capital=row["committed_capital"],
        accrued_rewards=row["accrued_rewards"],
        realized_bleed=row["realized_bleed"],
        fills=row["fills"],
        status=row["status"],
        last_mid=row["last_mid"],
        created_at=row["created_at"],
        last_accrued_at=row["last_accrued_at"],
    )
