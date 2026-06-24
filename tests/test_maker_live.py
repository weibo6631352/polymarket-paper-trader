"""Tests for the live maker-quoting infrastructure (dry-run safe)."""

from __future__ import annotations

import pytest

from pm_trader.maker_live import (
    LiveMakerBot,
    compute_two_sided_quotes,
    plan_requote,
)
from pm_trader.models import ApiError, OrderBook, OrderBookLevel


def _book(bid=0.49, ask=0.51, size=1000):
    return OrderBook(
        bids=[OrderBookLevel(price=bid, size=size)],
        asks=[OrderBookLevel(price=ask, size=size)],
    )


# ---------------------------------------------------------------------------
# compute_two_sided_quotes
# ---------------------------------------------------------------------------

class TestComputeQuotes:
    def test_basic(self):
        q = compute_two_sided_quotes(
            0.50, half_spread_c=1.0, size=50, tick=0.01, max_spread_c=4.5)
        assert q[0] == {"side": "BUY", "price": 0.49, "size": 50}
        assert q[1] == {"side": "SELL", "price": 0.51, "size": 50}

    def test_bad_mid(self):
        with pytest.raises(ValueError):
            compute_two_sided_quotes(0.0, half_spread_c=1.0, size=50, tick=0.01, max_spread_c=4.5)

    def test_bad_half_spread_zero(self):
        with pytest.raises(ValueError):
            compute_two_sided_quotes(0.5, half_spread_c=0.0, size=50, tick=0.01, max_spread_c=4.5)

    def test_bad_half_spread_too_wide(self):
        with pytest.raises(ValueError):
            compute_two_sided_quotes(0.5, half_spread_c=9.0, size=50, tick=0.01, max_spread_c=4.5)

    def test_clamps_near_edges(self):
        # mid near 0 → bid clamps to tick floor
        q = compute_two_sided_quotes(
            0.02, half_spread_c=4.0, size=50, tick=0.01, max_spread_c=4.5)
        assert q[0]["price"] >= 0.01
        # mid near 1 → ask clamps below 1
        q2 = compute_two_sided_quotes(
            0.98, half_spread_c=4.0, size=50, tick=0.01, max_spread_c=4.5)
        assert q2[1]["price"] <= 0.99


# ---------------------------------------------------------------------------
# plan_requote
# ---------------------------------------------------------------------------

class TestPlanRequote:
    def test_move_triggers(self):
        assert plan_requote(0.50, 0.52, half_spread_c=1.0, tick=0.01) is True

    def test_small_move_no_requote(self):
        assert plan_requote(0.50, 0.505, half_spread_c=1.0, tick=0.01) is False


# ---------------------------------------------------------------------------
# LiveMakerBot
# ---------------------------------------------------------------------------

class TestLiveMakerBot:
    def _bot(self, **kw):
        return LiveMakerBot(token_id="tok", max_spread_c=4.5, min_size=50.0,
                            tick=0.01, **kw)

    def test_dry_run_default(self):
        bot = self._bot()
        assert bot.dry_run is True
        assert bot.size == 50.0
        assert bot.half_spread_c == 1.0

    def test_live_without_submitter_raises(self):
        with pytest.raises(ApiError):
            self._bot(dry_run=False)

    def test_plan_estimates_share(self):
        bot = self._bot()
        plan = bot.plan(_book(), 0.50)
        assert 0 <= plan["est_reward_share"] <= 1
        assert plan["committed_capital"] == pytest.approx(49.0)
        assert plan["dry_run"] is True
        assert len(plan["orders"]) == 2

    def test_first_step_places_no_cancel(self):
        bot = self._bot()
        out = bot.step(_book(), 0.50)
        assert out["requote"] is True
        # first time: 2 places, no cancel
        actions = [s["action"] for s in out["submitted"]]
        assert actions == ["PLACE", "PLACE"]
        assert all(s["status"] == "DRY_RUN" for s in out["submitted"])

    def test_second_step_small_move_no_requote(self):
        bot = self._bot()
        bot.step(_book(), 0.50)
        out = bot.step(_book(0.495, 0.505), 0.50)  # mid unchanged
        assert out["requote"] is False
        assert out["submitted"] == []

    def test_big_move_cancels_and_reposts(self):
        bot = self._bot()
        bot.step(_book(), 0.50)
        out = bot.step(_book(0.58, 0.60), 0.59)  # 9c move
        assert out["requote"] is True
        actions = [s["action"] for s in out["submitted"]]
        assert actions == ["CANCEL_ALL", "PLACE", "PLACE"]

    def test_custom_submitter_used(self):
        calls = []

        def sub(action):
            calls.append(action)
            return {"status": "FAKE", **action}

        bot = self._bot(dry_run=False, submitter=sub)
        out = bot.step(_book(), 0.50)
        assert len(calls) == 2
        assert all(s["status"] == "FAKE" for s in out["submitted"])
