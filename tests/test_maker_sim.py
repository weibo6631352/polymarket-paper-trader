"""Tests for the maker-rewards paper simulator."""

from __future__ import annotations

import pytest

from pm_trader import maker_sim
from pm_trader.maker_sim import (
    _history_dt_seconds,
    _path_from_history,
    simulate_pool,
)


# ---------------------------------------------------------------------------
# simulate_pool
# ---------------------------------------------------------------------------

class TestSimulatePool:
    def test_flat_path_pure_reward(self):
        # mid never moves → no pickoffs → net == reward income
        path = [0.50] * 25  # 24 steps
        r = simulate_pool(
            path, daily=100.0, share=0.05, tick=0.01, min_size=50.0,
            dt_seconds=3600.0, cancel_efficiency=0.0)
        assert r["pickoffs"] == 0
        assert r["adverse_bleed"] == 0.0
        assert r["reward_income"] > 0
        assert r["net"] == r["reward_income"]
        assert r["net_annualized_pct"] > 0

    def test_pickoff_bleed(self):
        path = [0.50, 0.60]  # one 10c move >= 1c tick
        r = simulate_pool(
            path, daily=100.0, share=0.05, tick=0.01, min_size=50.0,
            dt_seconds=3600.0, cancel_efficiency=0.0)
        assert r["pickoffs"] == 1
        # loss = (0.10 - 0.01) * 50 = 4.5
        assert r["adverse_bleed"] == pytest.approx(4.5)

    def test_cancel_efficiency_reduces_bleed(self):
        path = [0.50, 0.60]
        slow = simulate_pool(path, daily=100.0, share=0.05, tick=0.01,
                             min_size=50.0, dt_seconds=3600.0, cancel_efficiency=0.0)
        fast = simulate_pool(path, daily=100.0, share=0.05, tick=0.01,
                             min_size=50.0, dt_seconds=3600.0, cancel_efficiency=0.9)
        assert fast["adverse_bleed"] == pytest.approx(slow["adverse_bleed"] * 0.1)
        assert fast["net"] > slow["net"]

    def test_small_move_no_pickoff(self):
        path = [0.50, 0.505]  # 0.5c < 1c tick
        r = simulate_pool(path, daily=100.0, share=0.05, tick=0.01,
                          min_size=50.0, dt_seconds=3600.0)
        assert r["pickoffs"] == 0

    def test_empty_path_zero_steps(self):
        r = simulate_pool([0.5], daily=100.0, share=0.05, tick=0.01,
                          min_size=50.0, dt_seconds=3600.0)
        assert r["steps"] == 0
        assert r["pickoff_rate"] == 0.0
        assert r["reward_income"] == 0.0

    def test_bad_cancel_efficiency(self):
        with pytest.raises(ValueError):
            simulate_pool([0.5, 0.5], daily=100.0, share=0.05, tick=0.01,
                          min_size=50.0, dt_seconds=3600.0, cancel_efficiency=1.5)

    def test_requote_downtime_reduces_reward(self):
        # one pickoff (10c move) + downtime → less reward than no downtime
        path = [0.50, 0.60, 0.60]
        base = simulate_pool(path, daily=100.0, share=0.5, tick=0.01,
                             min_size=50.0, dt_seconds=3600.0, cancel_efficiency=1.0)
        down = simulate_pool(path, daily=100.0, share=0.5, tick=0.01,
                             min_size=50.0, dt_seconds=3600.0, cancel_efficiency=1.0,
                             requote_downtime_s=1800.0)
        assert down["reward_income"] < base["reward_income"]

    def test_downtime_capped_at_horizon(self):
        # huge downtime can't drive reward below 0
        path = [0.50, 0.60]
        r = simulate_pool(path, daily=100.0, share=0.5, tick=0.01, min_size=50.0,
                          dt_seconds=60.0, cancel_efficiency=1.0,
                          requote_downtime_s=1e9)
        assert r["reward_income"] == 0.0

    def test_bad_downtime(self):
        with pytest.raises(ValueError):
            simulate_pool([0.5, 0.5], daily=100.0, share=0.05, tick=0.01,
                          min_size=50.0, dt_seconds=3600.0, requote_downtime_s=-1.0)

    def test_unwind_cost_reduces_net(self):
        path = [0.50, 0.60]  # one pickoff
        base = simulate_pool(path, daily=100.0, share=0.5, tick=0.01, min_size=50.0,
                             dt_seconds=3600.0)
        unwound = simulate_pool(path, daily=100.0, share=0.5, tick=0.01, min_size=50.0,
                                dt_seconds=3600.0, unwind_cost_ticks=2.0)
        # 2 ticks × 50 sh × $0.01 = $1.00 unwind on the single fill
        assert unwound["unwind_cost"] == pytest.approx(1.0)
        assert unwound["net"] == pytest.approx(base["net"] - 1.0)

    def test_unwind_scales_with_cancel_efficiency(self):
        path = [0.50, 0.60]
        slow = simulate_pool(path, daily=100.0, share=0.5, tick=0.01, min_size=50.0,
                             dt_seconds=3600.0, cancel_efficiency=0.0, unwind_cost_ticks=2.0)
        fast = simulate_pool(path, daily=100.0, share=0.5, tick=0.01, min_size=50.0,
                             dt_seconds=3600.0, cancel_efficiency=0.9, unwind_cost_ticks=2.0)
        assert fast["unwind_cost"] == pytest.approx(slow["unwind_cost"] * 0.1)

    def test_bad_unwind(self):
        with pytest.raises(ValueError):
            simulate_pool([0.5, 0.5], daily=100.0, share=0.05, tick=0.01,
                          min_size=50.0, dt_seconds=3600.0, unwind_cost_ticks=-1.0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class TestHistoryHelpers:
    def test_dt_seconds_median(self):
        hist = [{"t": 0}, {"t": 60}, {"t": 120}, {"t": 180}]
        assert _history_dt_seconds(hist) == 60.0

    def test_dt_seconds_fallback(self):
        assert _history_dt_seconds([{"t": 5}]) == 3600.0

    def test_dt_seconds_bad_points_skipped(self):
        hist = [{"t": "x"}, {"t": 100}, {"t": 160}]
        assert _history_dt_seconds(hist) == 60.0

    def test_path_extraction(self):
        assert _path_from_history([{"p": 0.5}, {"p": "bad"}, {"p": 0.6}]) == [0.5, 0.6]


# ---------------------------------------------------------------------------
# run_experiment / run — FakeClient
# ---------------------------------------------------------------------------

def _market(daily=100.0, token="tok", min_size=50.0, tick=0.01, q="Q?"):
    return {
        "rewards": {"rates": [{"rewards_daily_rate": daily}],
                    "max_spread": 4.5, "min_size": min_size},
        "minimum_tick_size": tick,
        "tokens": [{"token_id": token}],
        "question": q,
        "condition_id": "0xc",
    }


def _flat_hist(n=15, p=0.5, step=3600):
    return [{"t": i * step, "p": p} for i in range(n)]


def _two_sided(bid=0.49, ask=0.51, size=1000):
    return {"bids": [{"price": bid, "size": size}], "asks": [{"price": ask, "size": size}]}


class FakeClient:
    def __init__(self, markets, histories, errors=None, books=None, book_errors=None):
        self.markets = markets
        self.histories = histories
        self.errors = errors or set()
        self.books = books or {}
        self.book_errors = book_errors or set()
        self.closed = False

    def sampling_markets(self):
        return self.markets

    def prices_history(self, token, *, interval="max", fidelity=1440):
        if token in self.errors:
            raise RuntimeError("boom")
        return self.histories.get(token, [])

    def book(self, token):
        if token in self.book_errors:
            raise RuntimeError("book down")
        return self.books.get(token, {})

    def close(self):
        self.closed = True


class TestLivePoolShare:
    def _pool(self, token="a"):
        from pm_trader.rewards import parse_rewards
        return parse_rewards(_market(token=token))

    def test_measures_share(self):
        from pm_trader.maker_sim import live_pool_share
        fc = FakeClient([], {}, books={"a": _two_sided()})
        s = live_pool_share(fc, self._pool("a"))
        assert 0 < s < 1

    def test_empty_book_none(self):
        from pm_trader.maker_sim import live_pool_share
        fc = FakeClient([], {}, books={"a": {"bids": [], "asks": []}})
        assert live_pool_share(fc, self._pool("a")) is None

    def test_book_error_none(self):
        from pm_trader.maker_sim import live_pool_share
        fc = FakeClient([], {}, book_errors={"a"})
        assert live_pool_share(fc, self._pool("a")) is None


class TestRunExperiment:
    def test_basic_aggregate(self):
        markets = [_market(daily=100.0, token="a")]
        fc = FakeClient(markets, {"a": _flat_hist(20)})
        rep = maker_sim.run_experiment(fc, min_daily=50.0,
                                       cancel_efficiencies=(0.0, 0.9))
        assert rep["pools_simulated"] == 1
        assert "eff_0.0" in rep["aggregate"]
        assert "eff_0.9" in rep["aggregate"]
        assert rep["aggregate"]["eff_0.0"]["net"] > 0

    def test_filters_below_min_daily(self):
        markets = [_market(daily=10.0, token="lo"), _market(daily=100.0, token="hi")]
        fc = FakeClient(markets, {"hi": _flat_hist(20)})
        rep = maker_sim.run_experiment(fc, min_daily=50.0)
        assert rep["pools_simulated"] == 1

    def test_skips_short_history(self):
        markets = [_market(daily=100.0, token="a")]
        fc = FakeClient(markets, {"a": _flat_hist(5)})  # < 10 points
        rep = maker_sim.run_experiment(fc, min_daily=50.0)
        assert rep["pools_simulated"] == 0

    def test_skips_history_error(self):
        markets = [_market(daily=100.0, token="a")]
        fc = FakeClient(markets, {}, errors={"a"})
        rep = maker_sim.run_experiment(fc, min_daily=50.0)
        assert rep["pools_simulated"] == 0

    def test_top_cap(self):
        markets = [_market(daily=100.0 + i, token=f"t{i}") for i in range(4)]
        hist = {f"t{i}": _flat_hist(20) for i in range(4)}
        fc = FakeClient(markets, hist)
        rep = maker_sim.run_experiment(fc, min_daily=50.0, top=2)
        assert rep["pools_simulated"] == 2

    def test_scanned_share_used(self):
        markets = [_market(daily=100.0, token="a")]
        fc = FakeClient(markets, {"a": _flat_hist(20)}, books={"a": _two_sided()})
        rep = maker_sim.run_experiment(fc, min_daily=50.0, use_scanned_share=True)
        # share measured from the (contested) book, not the 0.05 flat default
        assert rep["pools"][0]["share_used"] != 0.05
        assert rep["params"]["use_scanned_share"] is True

    def test_flat_share_when_disabled(self):
        markets = [_market(daily=100.0, token="a")]
        fc = FakeClient(markets, {"a": _flat_hist(20)}, books={"a": _two_sided()})
        rep = maker_sim.run_experiment(fc, min_daily=50.0, share=0.05,
                                       use_scanned_share=False)
        assert rep["pools"][0]["share_used"] == 0.05

    def test_scanned_share_falls_back_on_empty_book(self):
        markets = [_market(daily=100.0, token="a")]
        fc = FakeClient(markets, {"a": _flat_hist(20)})  # no books → fallback
        rep = maker_sim.run_experiment(fc, min_daily=50.0, share=0.05,
                                       use_scanned_share=True)
        assert rep["pools"][0]["share_used"] == 0.05

    def test_unwind_cost_in_aggregate(self):
        # jumpy path so there are pickoffs to incur unwind cost
        markets = [_market(daily=100.0, token="a")]
        path = [{"t": i * 3600, "p": 0.5 + 0.1 * (i % 2)} for i in range(20)]
        fc = FakeClient(markets, {"a": path})
        rep = maker_sim.run_experiment(fc, min_daily=50.0, use_scanned_share=False,
                                       unwind_cost_ticks=2.0)
        assert rep["params"]["unwind_cost_ticks"] == 2.0
        assert rep["aggregate"]["eff_0.0"]["unwind_cost"] > 0


def test_run_builds_and_closes(monkeypatch):
    markets = [_market(daily=100.0, token="a")]
    fake = FakeClient(markets, {"a": _flat_hist(20)})
    monkeypatch.setattr(maker_sim, "RewardsClient", lambda: fake)
    rep = maker_sim.run(min_daily=50.0)
    assert rep["pools_simulated"] == 1
    assert fake.closed is True
