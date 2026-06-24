"""Tests for periodic market discovery (paginated full scan + scheduled refresh)."""

from __future__ import annotations

import json

from pm_trader import discovery


def _market(token, daily=400.0, min_size=50.0, tick=0.01):
    return {"rewards": {"rates": [{"rewards_daily_rate": daily}],
                        "max_spread": 4.5, "min_size": min_size},
            "minimum_tick_size": tick, "tokens": [{"token_id": token}],
            "question": f"Q-{token}?", "condition_id": "0x" + token}


def _flat_hist(n=15):
    return [{"t": i * 3600, "p": 0.5} for i in range(n)]


def _two_sided():
    return {"bids": [{"price": 0.49, "size": 1000}], "asks": [{"price": 0.51, "size": 1000}]}


class FakeClient:
    def __init__(self, markets, books, histories):
        self.markets = markets
        self.books = books
        self.histories = histories
        self.closed = False
        self.scans = 0

    def sampling_markets(self, *, max_pages=100):
        self.scans += 1
        return self.markets

    def book(self, token):
        return self.books.get(token, {})

    def prices_history(self, token, *, interval="max", fidelity=1440):
        return self.histories.get(token, [])

    def close(self):
        self.closed = True


def _client():
    markets = [_market("a"), _market("b")]
    return FakeClient(markets, {"a": _two_sided(), "b": _two_sided()},
                      {"a": _flat_hist(), "b": _flat_hist()})


class TestRefresh:
    def test_returns_safe_summary(self):
        out = discovery.refresh(_client(), min_daily=80.0)
        assert "safe_count" in out and out["pools_scored"] >= 1

    def test_writes_file(self, tmp_path):
        path = tmp_path / "latest.json"
        discovery.refresh(_client(), out_path=str(path), min_daily=80.0)
        saved = json.loads(path.read_text())
        assert "safe" in saved and "safe_count" in saved


class TestWatch:
    def test_loops_and_sleeps(self):
        sleeps = []
        c = _client()
        results = discovery.watch(c, interval_s=300.0, rounds=3,
                                  sleeper=lambda s: sleeps.append(s), min_daily=80.0)
        assert len(results) == 3
        assert c.scans == 3
        assert sleeps == [300.0, 300.0]   # rounds-1 sleeps

    def test_zero_rounds(self):
        c = _client()
        assert discovery.watch(c, interval_s=10.0, rounds=0, sleeper=lambda _: None) == []


def test_run_builds_and_closes(monkeypatch):
    fake = _client()
    monkeypatch.setattr(discovery, "RewardsClient", lambda: fake)
    out = discovery.run(watch_rounds=1, interval_s=10.0, min_daily=80.0)
    assert len(out) == 1
    assert fake.closed is True
