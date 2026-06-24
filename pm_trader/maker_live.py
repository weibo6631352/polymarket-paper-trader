"""Live maker-quoting infrastructure for the liquidity-rewards strategy.

This is the bridge from the validated PAPER strategy to a real CLOB market-making
bot.  It computes the two-sided quotes to rest in a reward pool and the
cancel/re-quote actions as the midpoint moves (the fast-cancel behaviour that, on
colocated infra, shrinks adverse selection).

SAFETY — real money is hard-gated:
  - ``dry_run=True`` is the default.  In dry-run the bot computes and returns the
    orders it WOULD place/cancel and submits NOTHING to any network.
  - Real submission requires ALL of: ``dry_run=False``, an injected ``signer``
    (a configured py-clob-client), and the operator's funded wallet.  The
    ``build_clob_signer`` factory lazily imports py-clob-client and refuses
    unless ``PM_TRADER_LIVE=1`` and a private key are present in the environment.
  - This module never funds a wallet or holds keys; the operator wires the signer
    and flips the switch.  Nothing here places a real order on its own.
"""

from __future__ import annotations

import os

from pm_trader.models import ApiError, OrderBook
from pm_trader.orderbook import book_inband_qmin, committed_capital, maker_reward_share


def compute_two_sided_quotes(
    mid: float,
    *,
    half_spread_c: float,
    size: float,
    tick: float,
    max_spread_c: float,
) -> list[dict]:
    """Return the two resting orders (a YES bid + a YES ask) to quote at ``mid``.

    Quotes rest ``half_spread_c`` cents either side of mid, rounded to ``tick``,
    clamped into [tick, 1-tick].  Raises if the offset is outside the reward band.
    """
    if not 0.0 < mid < 1.0:
        raise ValueError(f"mid must be in (0, 1), got {mid}")
    if half_spread_c <= 0 or half_spread_c > max_spread_c:
        raise ValueError(
            f"half_spread_c must be in (0, {max_spread_c}], got {half_spread_c}"
        )
    offset = half_spread_c / 100.0
    bid = max(tick, round((mid - offset) / tick) * tick)
    ask = min(1.0 - tick, round((mid + offset) / tick) * tick)
    return [
        {"side": "BUY", "price": round(bid, 4), "size": size},
        {"side": "SELL", "price": round(ask, 4), "size": size},
    ]


def plan_requote(
    mid_prev: float, mid_now: float, *, half_spread_c: float, tick: float
) -> bool:
    """Whether the mid moved enough to warrant cancelling and re-centering.

    A move of at least one tick means a resting quote is now off-center and (if
    the move is toward a side) at risk of being picked off — re-quote.
    """
    return abs(mid_now - mid_prev) >= tick


class LiveMakerBot:
    """Plans live maker quotes for one reward pool; submits only when ungated.

    ``submitter`` is a callable ``(action: dict) -> dict`` (inject a real
    CLOB-backed one for live; the default dry-run submitter returns a plan echo
    and touches no network).
    """

    def __init__(
        self,
        *,
        token_id: str,
        max_spread_c: float,
        min_size: float,
        tick: float,
        half_spread_c: float | None = None,
        size: float | None = None,
        dry_run: bool = True,
        submitter=None,
    ) -> None:
        self.token_id = token_id
        self.max_spread_c = max_spread_c
        self.min_size = min_size
        self.tick = tick
        self.half_spread_c = (tick * 100.0) if half_spread_c is None else half_spread_c
        self.size = min_size if size is None else size
        self.dry_run = dry_run
        if not dry_run and submitter is None:
            raise ApiError("Live mode requires an injected signer/submitter")
        self._submitter = submitter or self._dry_run_submit
        self.last_mid: float | None = None

    @staticmethod
    def _dry_run_submit(action: dict) -> dict:
        """Default submitter: echo the action as a planned (un-sent) order."""
        return {"status": "DRY_RUN", **action}

    def plan(self, book: OrderBook, mid: float) -> dict:
        """Compute the actions for this poll: (re)quote if needed, with reward est."""
        quotes = compute_two_sided_quotes(
            mid, half_spread_c=self.half_spread_c, size=self.size,
            tick=self.tick, max_spread_c=self.max_spread_c,
        )
        requote = self.last_mid is None or plan_requote(
            self.last_mid, mid, half_spread_c=self.half_spread_c, tick=self.tick
        )
        existing_qmin = book_inband_qmin(book, mid, self.max_spread_c)
        share = maker_reward_share(
            self.size, self.half_spread_c, self.max_spread_c, existing_qmin
        )
        return {
            "token_id": self.token_id,
            "mid": mid,
            "requote": requote,
            "orders": quotes,
            "est_reward_share": round(share, 4),
            "committed_capital": round(committed_capital(self.size, self.half_spread_c), 2),
            "dry_run": self.dry_run,
        }

    def step(self, book: OrderBook, mid: float) -> dict:
        """Plan and (only if requote needed) submit the cancel+repost actions."""
        plan = self.plan(book, mid)
        submitted = []
        if plan["requote"]:
            if self.last_mid is not None:
                submitted.append(self._submitter({"action": "CANCEL_ALL", "token_id": self.token_id}))
            for o in plan["orders"]:
                submitted.append(self._submitter({"action": "PLACE", "token_id": self.token_id, **o}))
        self.last_mid = mid
        plan["submitted"] = submitted
        return plan


def build_clob_signer():  # pragma: no cover - requires external lib + live creds
    """Build a real py-clob-client signer. Hard-gated; never used in paper runs.

    Refuses unless ``PM_TRADER_LIVE=1`` and a ``POLYMARKET_PRIVATE_KEY`` are set,
    and py-clob-client is installed.  Returns a submitter callable bound to a
    funded wallet.  Intentionally excluded from coverage — it touches real funds.
    """
    if os.environ.get("PM_TRADER_LIVE") != "1":
        raise ApiError("Refusing live signer: set PM_TRADER_LIVE=1 to opt in")
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not pk:
        raise ApiError("Refusing live signer: POLYMARKET_PRIVATE_KEY not set")
    try:
        from py_clob_client.client import ClobClient
    except ImportError as e:
        raise ApiError("py-clob-client not installed; pip install py-clob-client") from e
    client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137)

    def submit(action: dict) -> dict:
        return {"status": "LIVE_SUBMIT_NOT_IMPLEMENTED", **action}

    return submit
