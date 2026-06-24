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


class FakeClient:
    def __init__(self, markets, histories, errors=None):
        self.markets = markets
        self.histories = histories
        self.errors = errors or set()
        self.closed = False

    def sampling_markets(self):
        return self.markets

    def prices_history(self, token, *, interval="max", fidelity=1440):
        if token in self.errors:
            raise RuntimeError("boom")
        return self.histories.get(token, [])

    def close(self):
        self.closed = True


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


def test_run_builds_and_closes(monkeypatch):
    markets = [_market(daily=100.0, token="a")]
    fake = FakeClient(markets, {"a": _flat_hist(20)})
    monkeypatch.setattr(maker_sim, "RewardsClient", lambda: fake)
    rep = maker_sim.run(min_daily=50.0)
    assert rep["pools_simulated"] == 1
    assert fake.closed is True
