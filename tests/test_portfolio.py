"""Tests for the maker portfolio orchestrator."""

from __future__ import annotations

import pytest

from pm_trader import portfolio as pf
from pm_trader.models import OrderBook, OrderBookLevel
from pm_trader.portfolio import (
    MakerPortfolio,
    _cluster_key,
    _risk_adjusted_score,
    _significant_tokens,
    fetch_market_data,
    select_pools,
)


def _rich_pool(token, question, *, daily=400.0, share=0.3, min_size=50.0,
               days_wiped=1.0, reward_per_day=None):
    """A scan-style pool dict with the fields the new ranking/dedup use."""
    return {"question": question, "condition_id": "0x" + token, "token": token,
            "daily": daily, "share": share, "min_size": min_size, "tick": 0.01,
            "max_spread_c": 4.5, "jump_verdict": "SAFE", "empty_band": False,
            "days_wiped": days_wiped,
            "reward_per_day": reward_per_day if reward_per_day is not None else share * daily}


def _pool(token="t1", daily=400.0, share=0.3, min_size=50.0, tick=0.01,
          verdict="SAFE", empty=False, q="Q?"):
    return {
        "question": q, "condition_id": "0x" + token, "token": token,
        "daily": daily, "share": share, "min_size": min_size, "tick": tick,
        "max_spread_c": 4.5, "jump_verdict": verdict, "empty_band": empty,
    }


def _book(bid=0.49, ask=0.51, size=1000):
    return OrderBook(bids=[OrderBookLevel(price=bid, size=size)],
                     asks=[OrderBookLevel(price=ask, size=size)])


# ---------------------------------------------------------------------------
# select_pools
# ---------------------------------------------------------------------------

class TestSelectPools:
    def test_selects_safe_within_budget(self):
        rep = {"pools": [_pool("a"), _pool("b"), _pool("c")]}
        sel = select_pools(rep, capital=200.0)
        assert len(sel) == 3  # 3 × ~$49 < $200
        assert sel[0]["committed_capital"] == pytest.approx(49.0)
        assert sel[0]["est_daily_reward"] == pytest.approx(0.3 * 400)

    def test_budget_caps_count(self):
        rep = {"pools": [_pool(f"t{i}") for i in range(10)]}
        sel = select_pools(rep, capital=100.0)  # ~$49 each → only 2 fit
        assert len(sel) == 2

    def test_skips_non_safe(self):
        rep = {"pools": [_pool("a", verdict="KILL"), _pool("b", empty=True), _pool("c")]}
        sel = select_pools(rep, capital=200.0)
        assert [s["token"] for s in sel] == ["c"]

    def test_require_safe_false_includes_all(self):
        rep = {"pools": [_pool("a", verdict="WATCH"), _pool("b", verdict="KILL")]}
        sel = select_pools(rep, capital=200.0, require_safe=False)
        assert len(sel) == 2

    def test_cooldown_excludes_jumped_market(self):
        rep = {"pools": [_pool("a"), _pool("b"), _pool("c")]}
        # 'a' just had a catalyst jump → on cooldown by its condition_id ("0xa")
        sel = select_pools(rep, capital=200.0, cooldown={"0xa"})
        tokens = [s["token"] for s in sel]
        assert "a" not in tokens and "b" in tokens and "c" in tokens

    def test_skips_degenerate_capital(self):
        # half_spread_ticks huge → committed_capital 0 → skipped
        rep = {"pools": [_pool("a", tick=0.30)]}
        sel = select_pools(rep, capital=200.0, half_spread_ticks=2)  # 60c half-spread → cap 0
        assert sel == []

    def test_max_pools_limit(self):
        rep = {"pools": [_pool(f"t{i}", min_size=1.0) for i in range(10)]}
        sel = select_pools(rep, capital=1000.0, max_pools=3)
        assert len(sel) == 3


# ---------------------------------------------------------------------------
# MakerPortfolio
# ---------------------------------------------------------------------------

class TestSelectionCriteria:
    def test_significant_tokens_filters_noise(self):
        toks = _significant_tokens("Will Trump meet with Putin in 2026?")
        assert "trump" in toks and "putin" in toks
        assert "will" not in toks and "with" not in toks and "2026" not in toks

    def test_risk_adjusted_score_prefers_safer_per_reward(self):
        jumpy = _rich_pool("a", "X", reward_per_day=10.0, days_wiped=6.0)
        safe = _rich_pool("b", "Y", reward_per_day=8.0, days_wiped=0.5)
        assert _risk_adjusted_score(safe, 7.0) > _risk_adjusted_score(jumpy, 7.0)

    def test_risk_adjusted_score_fallbacks(self):
        # no reward_per_day → share*daily; no days_wiped → heavy penalty
        p = {"share": 0.1, "daily": 100.0}
        assert _risk_adjusted_score(p, 7.0) > 0

    def test_ranks_by_risk_adjusted_not_raw_reward(self):
        # only ~one $98 quote fits in $100 → the safer-per-reward pool should win
        jumpy = _rich_pool("a", "Alpha", min_size=100.0, reward_per_day=10.0, days_wiped=6.0)
        safe = _rich_pool("b", "Bravo", min_size=100.0, reward_per_day=8.0, days_wiped=0.5)
        sel = select_pools({"pools": [jumpy, safe]}, capital=100.0)
        assert len(sel) == 1 and sel[0]["token"] == "b"

    def test_correlation_dedup_skips_same_event(self):
        pools = [
            _rich_pool("a", "Will Trump meet with Putin in 2026?"),
            _rich_pool("b", "Will Trump meet with Kim Jong Un in 2026?"),  # shares trump+meet
            _rich_pool("c", "Clarity Act signed into law in 2026?"),       # independent
        ]
        sel = select_pools({"pools": pools}, capital=200.0)
        tokens = [s["token"] for s in sel]
        assert "c" in tokens                      # independent kept
        assert not ("a" in tokens and "b" in tokens)  # the correlated pair not both

    def test_cluster_key_maps_themes(self):
        assert _cluster_key("Will Sorin Grindeanu be the next prime minister?") == "romania"
        assert _cluster_key("Romanian PM Bolojan out by June 30?") == "romania"
        assert _cluster_key("Fed rate hike by September 2026 meeting?") == "us-fed"
        assert _cluster_key("Will Gustavo Petro be the next leader out?") == "colombia"
        assert _cluster_key("Will Bitcoin hit $100k?") is None

    def test_cluster_dedup_catches_same_topic_different_words(self):
        # these share NO distinctive tokens but are the same event/country/topic
        pools = [
            _rich_pool("ro1", "Will Sorin Grindeanu be the next prime minister?"),
            _rich_pool("ro2", "Romanian PM Bolojan out by June 30?"),       # romania (no shared token)
            _rich_pool("fed1", "Fed rate hike by September 2026 meeting?"),
            _rich_pool("fed2", "Will the Fed pause in the next meeting?"),   # us-fed (shares only 'fed')
            _rich_pool("co1", "Will the central bank of Colombia increase rates?"),
            _rich_pool("co2", "Will Gustavo Petro be the next leader out?"), # colombia (no shared token)
            _rich_pool("ind", "Will Bitcoin hit $100k?"),                    # independent
        ]
        sel = select_pools({"pools": pools}, capital=2000.0)
        toks = [s["token"] for s in sel]
        assert sum(t in ("ro1", "ro2") for t in toks) == 1   # one Romania
        assert sum(t in ("fed1", "fed2") for t in toks) == 1  # one Fed
        assert sum(t in ("co1", "co2") for t in toks) == 1    # one Colombia
        assert "ind" in toks


class TestMakerPortfolio:
    def test_aggregates(self):
        sel = select_pools({"pools": [_pool("a"), _pool("b")]}, capital=200.0)
        port = MakerPortfolio(sel)
        assert port.summary()["pools"] == 2
        assert port.committed_capital() == pytest.approx(98.0)
        assert port.est_daily_reward() == pytest.approx(2 * 0.3 * 400)
        assert port.summary()["est_annualized_pct"] > 0

    def test_empty_portfolio_zero(self):
        port = MakerPortfolio([])
        assert port.summary()["est_annualized_pct"] == 0.0
        assert port.committed_capital() == 0.0

    def test_plan_all(self):
        sel = select_pools({"pools": [_pool("a"), _pool("b")]}, capital=200.0)
        port = MakerPortfolio(sel)
        md = {"a": (_book(), 0.50), "b": (_book(), 0.50)}
        plans = port.plan_all(md)
        assert len(plans) == 2
        assert all(len(p["orders"]) == 2 for p in plans)

    def test_plan_all_skips_missing_market_data(self):
        sel = select_pools({"pools": [_pool("a"), _pool("b")]}, capital=200.0)
        port = MakerPortfolio(sel)
        plans = port.plan_all({"a": (_book(), 0.50)})  # only 'a'
        assert len(plans) == 1


# ---------------------------------------------------------------------------
# fetch_market_data + build_portfolio + run
# ---------------------------------------------------------------------------

def _market(daily=400.0, token="t1", min_size=50.0, tick=0.01, q="Q?"):
    return {
        "rewards": {"rates": [{"rewards_daily_rate": daily}],
                    "max_spread": 4.5, "min_size": min_size},
        "minimum_tick_size": tick,
        "tokens": [{"token_id": token}],
        "question": q, "condition_id": "0x" + token,
    }


class FakeClient:
    def __init__(self, markets=None, books=None, histories=None, book_errors=None):
        self.markets = markets or []
        self.books = books or {}
        self.histories = histories or {}
        self.book_errors = book_errors or set()
        self.closed = False

    def sampling_markets(self):
        return self.markets

    def book(self, token_id):
        if token_id in self.book_errors:
            raise RuntimeError("book down")
        return self.books.get(token_id, {})

    def prices_history(self, token_id, *, interval="max", fidelity=1440):
        return self.histories.get(token_id, [])

    def close(self):
        self.closed = True


def _two_sided(bid=0.49, ask=0.51, size=1000):
    return {"bids": [{"price": bid, "size": size}], "asks": [{"price": ask, "size": size}]}


class TestFetchMarketData:
    def test_builds_orderbooks(self):
        sel = [{"token": "a"}]
        client = FakeClient(books={"a": _two_sided()})
        md = fetch_market_data(client, sel)
        assert "a" in md
        book, mid = md["a"]
        assert mid == pytest.approx(0.50)

    def test_skips_book_error(self):
        client = FakeClient(book_errors={"a"})
        assert fetch_market_data(client, [{"token": "a"}]) == {}

    def test_skips_one_sided(self):
        client = FakeClient(books={"a": {"bids": [], "asks": [{"price": 0.5, "size": 1}]}})
        assert fetch_market_data(client, [{"token": "a"}]) == {}


class TestBuildPortfolio:
    def _flat_hist(self, n=15):
        return [{"t": i * 3600, "p": 0.5} for i in range(n)]

    def test_end_to_end(self):
        markets = [_market(token="a"), _market(token="b")]
        client = FakeClient(
            markets=markets,
            books={"a": _two_sided(), "b": _two_sided()},
            histories={"a": self._flat_hist(), "b": self._flat_hist()},
        )
        out = pf.build_portfolio(client, capital=200.0, min_daily=80.0)
        assert out["summary"]["pools"] >= 1
        assert out["planned"] >= 1
        assert out["capital_budget"] == 200.0


def test_run_builds_and_closes(monkeypatch):
    markets = [_market(token="a")]
    fake = FakeClient(markets=markets, books={"a": _two_sided()},
                      histories={"a": [{"t": i * 3600, "p": 0.5} for i in range(15)]})
    monkeypatch.setattr(pf, "RewardsClient", lambda: fake)
    out = pf.run(capital=200.0)
    assert out["summary"]["pools"] >= 1
    assert fake.closed is True
