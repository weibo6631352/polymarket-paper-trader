"""Order book fill simulation engine.

Walks a real Polymarket order book level-by-level to compute exact execution
prices, slippage, and fees.  This is the core of pm-trader's 1:1 faithful
trade simulation.
"""

from __future__ import annotations

import math

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
    size: float,
    half_spread_c: float,
    mid_prev: float,
    mid_now: float,
    cancel_efficiency: float = 0.0,
) -> float:
    """Per-poll adverse-selection loss for a re-centering (no-inventory) maker.

    A move within ``half_spread_c`` of mid is harmless; a larger move fills the
    stale side at its quote price before the re-center, costing
    ``size * (|move| - half_spread) * (1 - cancel_efficiency)``.  This is the
    mark-to-market approximation used by the ``maker_sim`` backtester (which
    re-centers each step and never holds inventory).  The stateful engine uses
    :func:`maker_fill` instead, which accumulates inventory and marks it.
    """
    offset = half_spread_c / 100.0
    excess = abs(mid_now - mid_prev) - offset
    if excess <= 0:
        return 0.0
    return size * excess * max(0.0, 1.0 - cancel_efficiency)


def skewed_center(
    mid: float, inventory: float, size: float, half_spread_c: float,
    skew_strength: float,
) -> float:
    """Inventory-skewed quote center — lean away from inventory to flatten it.

    ``center = mid − skew_strength · (inventory/size) · offset`` (offset in price).
    Long (inventory > 0) → center DOWN, so the ask is keener to be lifted (sell,
    get flat) and the bid less keen to be hit; short → center UP.  This is the
    Avellaneda-Stoikov / Ho-Stoll reservation-price idea: quote around an
    inventory-adjusted fair value so the book mean-reverts toward flat.
    """
    offset = half_spread_c / 100.0
    return mid - skew_strength * (inventory / size) * offset if size > 0 else mid


def maker_fill(
    mid_prev: float,
    mid_now: float,
    inventory: float,
    size: float,
    half_spread_c: float,
    skew_strength: float,
    cancel_efficiency: float,
    max_inventory: float,
) -> tuple[float, float]:
    """Simulate the fill of the resting (skewed) quote as the mid moves prev→now.

    The quote rested ``half_spread_c`` cents either side of the skewed center
    computed at ``mid_prev`` with the pre-move ``inventory``.  When the mid drops
    through the bid we buy (inventory ↑); when it rises through the ask we sell
    (inventory ↓).  Fill size is ``size·(1−cancel_efficiency)`` — a faster
    canceller pulls more of the stale side before it trades — and is clamped so
    ``|inventory|`` never exceeds ``max_inventory`` (the position cap → one-sided
    quoting at the cap).

    Returns ``(delta_inventory, fill_loss)``: the signed inventory change and the
    adverse-selection cost of the fill (``≥ 0``, the picked-off amount marked at
    the new mid).
    """
    offset = half_spread_c / 100.0
    center = skewed_center(mid_prev, inventory, size, half_spread_c, skew_strength)
    bid, ask = center - offset, center + offset
    f = size * max(0.0, 1.0 - cancel_efficiency)
    if mid_now <= bid:                                   # bid hit → buy
        f = min(f, max(0.0, max_inventory - inventory))
        return f, f * (bid - mid_now)
    if mid_now >= ask:                                   # ask lifted → sell
        f = min(f, max(0.0, max_inventory + inventory))
        return -f, f * (mid_now - ask)
    return 0.0, 0.0


def committed_capital(size: float, half_spread_c: float) -> float:
    """Cash locked by a two-sided ``size`` quote (a YES bid + a NO bid).

    bid notional + ask notional = ``size*(mid - s) + size*(1 - mid - s)``
    ``= size * (1 - 2s)`` — independent of mid (``s`` in price units).  Clamped
    to 0 for degenerate quotes wider than 50c per side.
    """
    cap = size * (1.0 - 2.0 * (half_spread_c / 100.0))
    return cap if cap > 0 else 0.0


# ---------------------------------------------------------------------------
# Volatility-aware optimal quoting (Avellaneda-Stoikov, adapted to the subsidy)
# ---------------------------------------------------------------------------
#
# Classic MM (Avellaneda-Stoikov) widens the spread with volatility to dodge
# adverse selection: optimal half-spread ≈ inventory/vol term + fill-rate term.
# Our case is INVERTED by the reward subsidy — PM pays a reward share that grows
# QUADRATICALLY as the quote tightens (((c−s)/c)²), so there is a force pulling
# us tighter that classic MM lacks.  The optimal offset therefore balances:
#   reward(s)  — rises as s → 0 (bigger Qmin share of the daily pool), and
#   bleed(s)   — also rises as s → 0 (more mid moves cross the quote and pick it
#                off), scaling with volatility and shrunk by cancel_efficiency.
# We grid-search s ∈ [tick, max_spread] for the net-maximising offset.


def expected_excess_move(sigma: float, offset: float) -> float:
    """E[max(|Δ| − offset, 0)] for a zero-mean Gaussian move with std ``sigma``.

    Closed form for ``X ~ N(0, sigma²)`` and ``offset ≥ 0``::

        E[(|X| − a)+] = 2·sigma·φ(a/sigma) − 2·a·(1 − Φ(a/sigma))

    (φ = standard-normal PDF, Φ = its CDF).  This is the per-period adverse move
    beyond a quote resting ``offset`` from mid — the bleed driver.  ``sigma`` and
    ``offset`` share units (price or cents).  Returns 0 for non-positive sigma.
    """
    if sigma <= 0:
        return 0.0
    a = offset / sigma
    phi = math.exp(-0.5 * a * a) / math.sqrt(2.0 * math.pi)
    cdf = 0.5 * (1.0 + math.erf(a / math.sqrt(2.0)))
    return max(0.0, 2.0 * sigma * phi - 2.0 * offset * (1.0 - cdf))


def realized_sigma_c_from_history(history: list[dict], poll_seconds: float) -> float:
    """Per-poll mid-move volatility (cents) from CLOB ``prices-history`` points.

    Estimates the std of consecutive mid moves at the history's own cadence
    (median timestamp gap), then scales to the ``poll_seconds`` re-quote interval
    by random-walk √-time scaling.  Returns 0.0 when the path or cadence is
    degenerate (treated as no measured risk → recommends the tightest quote).
    """
    prices: list[float] = []
    ts: list[float] = []
    for pt in history:
        try:
            prices.append(float(pt["p"]))
            ts.append(float(pt["t"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(prices) < 2 or poll_seconds <= 0:
        return 0.0
    gaps = sorted(ts[i] - ts[i - 1] for i in range(1, len(ts)) if ts[i] > ts[i - 1])
    if not gaps:
        return 0.0
    step = gaps[len(gaps) // 2]  # all gaps are positive by construction
    diffs_c = [(prices[i] - prices[i - 1]) * 100.0 for i in range(1, len(prices))]
    n = len(diffs_c)
    mean = sum(diffs_c) / n
    var = sum((d - mean) ** 2 for d in diffs_c) / n
    return (var ** 0.5) * (poll_seconds / step) ** 0.5


def optimal_half_spread(
    *,
    daily_rate: float,
    max_spread_c: float,
    min_size: float,
    tick_c: float,
    existing_qmin: float,
    sigma_c: float,
    periods_per_day: float,
    cancel_efficiency: float = 0.0,
    grid: int = 200,
) -> dict:
    """Grid-search the half-spread (cents) that maximises net daily maker yield.

    ``net(s) = reward(s) − bleed(s)`` where
    ``reward(s) = daily_rate · own(s)/(own(s)+existing_qmin)`` with
    ``own(s) = min_size·((c−s)/c)²``, and
    ``bleed(s) = (1−eff)·min_size·E[(|Δ|−s)+]·periods_per_day`` for
    ``Δ ~ N(0, sigma_c²)``.  Searches ``s ∈ [tick_c, max_spread_c]`` and returns
    the best offset plus its net/reward/bleed/share.  With ``sigma_c = 0`` the
    bleed term vanishes and the tightest quote (``tick_c``) wins — the pure
    reward-maximising case.
    """
    lo, hi = tick_c, max_spread_c
    if grid < 1:
        grid = 1
    if hi <= lo:
        candidates = [lo]
    else:
        step = (hi - lo) / grid
        candidates = [lo + i * step for i in range(grid + 1)]

    best: dict | None = None
    for s in candidates:
        own = maker_quote_score(min_size, s, max_spread_c)
        denom = own + existing_qmin
        share = (own / denom) if denom > 0 else 0.0
        reward = share * daily_rate
        bleed = (
            max(0.0, 1.0 - cancel_efficiency)
            * min_size
            * (expected_excess_move(sigma_c, s) / 100.0)
            * periods_per_day
        )
        net = reward - bleed
        if best is None or net > best["net_per_day"]:
            best = {
                "half_spread_c": round(s, 4),
                "net_per_day": round(net, 4),
                "reward_per_day": round(reward, 4),
                "bleed_per_day": round(bleed, 4),
                "share": round(share, 4),
            }
    return best


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
