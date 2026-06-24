"""Order book fill simulation engine.

Walks a real Polymarket order book level-by-level to compute exact execution
prices, slippage, and fees.  This is the core of pm-trader's 1:1 faithful
trade simulation.
"""

from __future__ import annotations

from pm_trader.models import Fill, FillResult, OrderBook


# ---------------------------------------------------------------------------
# Fee calculation — exact Polymarket formula
# ---------------------------------------------------------------------------

def calculate_fee(fee_rate_bps: int, price: float, size: float) -> float:
    """Return the trading fee using the exact Polymarket formula.

    Formula: (fee_rate_bps / 10_000) * min(price, 1 - price) * size

    The fee is proportional to how close the price is to 0.50 (maximum
    uncertainty).  At extreme prices (near 0 or 1) the fee approaches zero.

    A minimum fee of 0.0001 is enforced when fee_rate_bps > 0 and the
    computed fee is positive.
    """
    if fee_rate_bps == 0:
        return 0.0

    fee = (fee_rate_bps / 10_000) * min(price, 1.0 - price) * size

    if fee > 0.0:
        fee = max(fee, 0.0001)

    return fee


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _midpoint(book: OrderBook) -> float | None:
    """Return (best_bid + best_ask) / 2, or None if either side is empty."""
    if not book.bids or not book.asks:
        return None

    best_bid = max(level.price for level in book.bids)
    best_ask = min(level.price for level in book.asks)
    return (best_bid + best_ask) / 2.0


def _empty_fill_result() -> FillResult:
    """Return a FillResult representing no execution."""
    return FillResult(
        filled=False,
        avg_price=0.0,
        total_cost=0.0,
        total_shares=0.0,
        fee=0.0,
        slippage_bps=0.0,
        levels_filled=0,
        is_partial=False,
        fills=[],
    )


# ---------------------------------------------------------------------------
# Buy simulation — walk the ASK side
# ---------------------------------------------------------------------------

def simulate_buy_fill(
    book: OrderBook,
    amount_usd: float,
    fee_rate_bps: int,
    order_type: str = "fok",
    max_price: float | None = None,
) -> FillResult:
    """Simulate buying shares by spending *amount_usd*.

    Walks the ASK side of the order book from lowest price upward, consuming
    liquidity level-by-level.

    Parameters
    ----------
    book:
        The current order book snapshot.
    amount_usd:
        Total USD to spend on shares (before fees).
    fee_rate_bps:
        Market fee rate in basis points.
    order_type:
        ``"fok"`` (fill-or-kill: all or nothing) or
        ``"fak"`` (fill-and-kill: partial fills allowed).
    max_price:
        If set, skip ask levels priced above this limit.

    Returns
    -------
    FillResult
        Detailed execution result including per-level fills.
    """
    if not book.asks:
        if order_type == "fok":
            return _empty_fill_result()
        return _empty_fill_result()

    sorted_asks = sorted(book.asks, key=lambda lvl: lvl.price)

    remaining_usd = amount_usd
    fills: list[Fill] = []

    for level_idx, level in enumerate(sorted_asks):
        if remaining_usd <= 0:
            break

        # Limit order: skip levels above max_price
        if max_price is not None and level.price > max_price:
            break

        max_cost_at_level = level.size * level.price

        if max_cost_at_level <= remaining_usd:
            # Consume the entire level
            fills.append(Fill(
                price=level.price,
                shares=level.size,
                cost=max_cost_at_level,
                level=level_idx + 1,
            ))
            remaining_usd -= max_cost_at_level
        else:
            # Partial level fill — buy as many shares as remaining USD allows
            shares = remaining_usd / level.price
            fills.append(Fill(
                price=level.price,
                shares=shares,
                cost=remaining_usd,
                level=level_idx + 1,
            ))
            remaining_usd = 0.0
            break

    if not fills:
        return _empty_fill_result()

    total_cost = sum(f.cost for f in fills)
    total_shares = sum(f.shares for f in fills)

    # FOK: reject if the book could not absorb the full amount
    is_partial = remaining_usd > 0
    if order_type == "fok" and is_partial:
        return _empty_fill_result()

    avg_price = total_cost / total_shares if total_shares > 0 else 0.0
    fee = calculate_fee(fee_rate_bps, avg_price, total_cost)

    midpoint = _midpoint(book)
    if midpoint and midpoint > 0:
        slippage_bps = (avg_price - midpoint) / midpoint * 10_000
    else:
        slippage_bps = 0.0

    return FillResult(
        filled=not is_partial,
        avg_price=avg_price,
        total_cost=total_cost,
        total_shares=total_shares,
        fee=fee,
        slippage_bps=slippage_bps,
        levels_filled=len(fills),
        is_partial=is_partial,
        fills=fills,
    )


# ---------------------------------------------------------------------------
# Liquidity-rewards maker simulation (paper)
# ---------------------------------------------------------------------------
#
# Polymarket pays a fixed daily USDC pool, from its own treasury, to resting
# limit orders quoted within ``max_spread`` cents of the midpoint (two-sided,
# size-weighted by ``((c - s) / c) ** 2`` where ``s`` is the order's distance
# from mid in cents and ``c`` is ``max_spread``).  A maker's reward share is its
# binding-side (lighter of bid/ask) score over the total in-band score.
#
# These pure functions mirror ``pm_trader.rewards``' scanner scoring on the
# engine's ``OrderBook`` dataclass so resting maker quotes can (a) accrue a share
# of the pool over their in-band uptime and (b) get adversely picked off when the
# mid jumps through them.  Net maker P&L = reward accrual − adverse bleed; the
# validated edge (see lp-rewards-edge) is that low-catalyst mid-tail pools net
# positive while deep marquee pools are a kill (one jump wipes weeks of reward).


def _inband_weight(s_cents: float, max_spread_c: float) -> float:
    """PM size-weight ``((c - s) / c) ** 2`` for an order ``s`` cents from mid.

    Returns 0 when the order is outside the band (``s < 0`` means it crosses the
    mid, ``s > c`` means it is too wide) or when ``max_spread_c <= 0``.
    """
    if max_spread_c <= 0:
        return 0.0
    if s_cents < -1e-9 or s_cents > max_spread_c + 1e-9:
        return 0.0
    return ((max_spread_c - s_cents) / max_spread_c) ** 2


def book_inband_qmin(book: OrderBook, mid: float, max_spread_c: float) -> float:
    """Binding-side (min of bid/ask) in-band reward score of the live book.

    Sums each side's ``size * weight`` over the levels within ``max_spread_c`` of
    ``mid``, then returns the lighter side — the competing makers' Qmin, used as
    the denominator term when estimating our own reward share.
    """
    bid_score = sum(
        lvl.size * _inband_weight((mid - lvl.price) * 100.0, max_spread_c)
        for lvl in book.bids
    )
    ask_score = sum(
        lvl.size * _inband_weight((lvl.price - mid) * 100.0, max_spread_c)
        for lvl in book.asks
    )
    return min(bid_score, ask_score)


def maker_quote_score(size: float, half_spread_c: float, max_spread_c: float) -> float:
    """Binding-side reward score of our own two-sided quote.

    Both sides rest ``half_spread_c`` cents from mid with ``size`` shares, so the
    bid and ask scores are equal and the binding (min) score is just one side.
    A quote wider than ``max_spread_c`` scores 0 (earns no reward).
    """
    return size * _inband_weight(half_spread_c, max_spread_c)


def maker_reward_share(
    size: float, half_spread_c: float, max_spread_c: float, existing_qmin: float
) -> float:
    """Our share of the daily pool ≈ ``our Qmin / (our Qmin + existing Qmin)``.

    Returns 0 if our quote is out of band (score 0); returns 1 against an empty
    in-band book (``existing_qmin == 0``).
    """
    mine = maker_quote_score(size, half_spread_c, max_spread_c)
    denom = mine + existing_qmin
    return (mine / denom) if denom > 0 else 0.0


def reward_accrual(share: float, daily_rate: float, seconds: float) -> float:
    """USDC reward for resting in-band for ``seconds`` at a given pool share."""
    if share <= 0 or daily_rate <= 0 or seconds <= 0:
        return 0.0
    return share * daily_rate * (seconds / 86_400.0)


def adverse_bleed(
    size: float, half_spread_c: float, mid_prev: float, mid_now: float
) -> float:
    """Adverse-selection loss when the mid moves past a resting quote side.

    A continuous maker re-centers each poll, so a move within ``half_spread_c``
    of mid is harmless (the validated minute-level pick rate is ~0).  A larger
    move fills the stale side at its quote price before the re-center, costing
    ``size * (|move| - half_spread)`` — the portion of the move beyond the quoted
    offset.  This is the discrete-jump bleed that kills deep marquee pools.
    """
    offset = half_spread_c / 100.0
    excess = abs(mid_now - mid_prev) - offset
    return size * excess if excess > 0 else 0.0


def committed_capital(size: float, half_spread_c: float) -> float:
    """Cash locked by a two-sided ``size`` quote (a YES bid + a NO bid).

    bid notional + ask notional = ``size*(mid - s) + size*(1 - mid - s)``
    ``= size * (1 - 2s)`` — independent of mid (``s`` in price units).  Clamped
    to 0 for degenerate quotes wider than 50c per side.
    """
    cap = size * (1.0 - 2.0 * (half_spread_c / 100.0))
    return cap if cap > 0 else 0.0


# ---------------------------------------------------------------------------
# Sell simulation — walk the BID side
# ---------------------------------------------------------------------------

def simulate_sell_fill(
    book: OrderBook,
    shares: float,
    fee_rate_bps: int,
    order_type: str = "fok",
    min_price: float | None = None,
) -> FillResult:
    """Simulate selling *shares* into the order book.

    Walks the BID side of the order book from highest price downward,
    consuming liquidity level-by-level.

    Parameters
    ----------
    book:
        The current order book snapshot.
    shares:
        Number of shares to sell.
    fee_rate_bps:
        Market fee rate in basis points.
    order_type:
        ``"fok"`` (fill-or-kill) or ``"fak"`` (fill-and-kill).
    min_price:
        If set, skip bid levels priced below this limit.

    Returns
    -------
    FillResult
        Detailed execution result including per-level fills.
    """
    if not book.bids:
        if order_type == "fok":
            return _empty_fill_result()
        return _empty_fill_result()

    sorted_bids = sorted(book.bids, key=lambda lvl: lvl.price, reverse=True)

    remaining_shares = shares
    fills: list[Fill] = []

    for level_idx, level in enumerate(sorted_bids):
        if remaining_shares <= 0:
            break

        # Limit order: skip levels below min_price
        if min_price is not None and level.price < min_price:
            break

        if level.size <= remaining_shares:
            # Consume entire level
            cost = level.size * level.price
            fills.append(Fill(
                price=level.price,
                shares=level.size,
                cost=cost,
                level=level_idx + 1,
            ))
            remaining_shares -= level.size
        else:
            # Partial level fill — sell only the remaining shares
            cost = remaining_shares * level.price
            fills.append(Fill(
                price=level.price,
                shares=remaining_shares,
                cost=cost,
                level=level_idx + 1,
            ))
            remaining_shares = 0.0
            break

    if not fills:
        return _empty_fill_result()

    total_cost = sum(f.cost for f in fills)
    total_shares = sum(f.shares for f in fills)

    # FOK: reject if the book could not absorb all shares
    is_partial = remaining_shares > 0
    if order_type == "fok" and is_partial:
        return _empty_fill_result()

    avg_price = total_cost / total_shares if total_shares > 0 else 0.0
    fee = calculate_fee(fee_rate_bps, avg_price, total_shares)

    midpoint = _midpoint(book)
    if midpoint and midpoint > 0:
        # Selling below midpoint means negative slippage
        slippage_bps = (avg_price - midpoint) / midpoint * 10_000
    else:
        slippage_bps = 0.0

    return FillResult(
        filled=not is_partial,
        avg_price=avg_price,
        total_cost=total_cost,
        total_shares=total_shares,
        fee=fee,
        slippage_bps=slippage_bps,
        levels_filled=len(fills),
        is_partial=is_partial,
        fills=fills,
    )
