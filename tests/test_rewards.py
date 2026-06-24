"""Tests for the liquidity-rewards pool scanner."""

from __future__ import annotations

import httpx
import pytest

from pm_trader import rewards
from pm_trader.rewards import (
    CLOB_BASE,
    RewardsClient,
    _best,
    classify_jump_risk,
    inband_score,
    parse_rewards,
    reward_share,
    score_pool,
)
from pm_trader.models import ApiError

TOKEN = "tok123"


def _market(daily=100.0, max_spread=4.5, min_size=50.0, tick=0.01, token=TOKEN,
            question="Q?", cid="0xc", rates=None, tokens=None):
    """Build a CLOB market dict with a reward config."""
    if rates is None:
        rates = [{"rewards_daily_rate": daily}]
    if tokens is None:
        tokens = [{"token_id": token}] if token else []
    return {
        "rewards": {"rates": rates, "max_spread": max_spread, "min_size": min_size},
        "minimum_tick_size": tick,
        "tokens": tokens,
        "question": question,
        "condition_id": cid,
    }


# ---------------------------------------------------------------------------
# parse_rewards
# ---------------------------------------------------------------------------

class TestParseRewards:
    def test_success(self):
        p = parse_rewards(_market(daily=100.0))
        assert p["daily"] == 100.0
        assert p["token"] == TOKEN
        assert p["max_spread"] == 4.5
        assert p["min_size"] == 50.0
        assert p["tick"] == 0.01

    def test_no_rewards(self):
        assert parse_rewards({"tokens": [{"token_id": TOKEN}]}) is None

    def test_zero_daily(self):
        assert parse_rewards(_market(rates=[{"rewards_daily_rate": 0}])) is None

    def test_multiple_rates_summed(self):
        p = parse_rewards(_market(rates=[
            {"rewards_daily_rate": 30}, {"rewards_daily_rate": 70}]))
        assert p["daily"] == 100.0

    def test_bad_rate_value_skipped(self):
        p = parse_rewards(_market(rates=[
            {"rewards_daily_rate": "nan-ish"}, {"rewards_daily_rate": 50}]))
        assert p["daily"] == 50.0

    def test_all_rates_bad_is_none(self):
        assert parse_rewards(_market(rates=[{"rewards_daily_rate": None}])) is None

    def test_no_token(self):
        assert parse_rewards(_market(token=None, tokens=[])) is None

    def test_token_without_id_skipped(self):
        m = _market(tokens=[{"token_id": ""}, {"token_id": TOKEN}])
        assert parse_rewards(m)["token"] == TOKEN

    def test_bad_config_value(self):
        m = _market()
        m["rewards"]["max_spread"] = "bad"
        assert parse_rewards(m) is None

    def test_question_truncated(self):
        p = parse_rewards(_market(question="x" * 200))
        assert len(p["question"]) == 80


# ---------------------------------------------------------------------------
# inband_score
# ---------------------------------------------------------------------------

class TestInbandScore:
    def test_in_band_contributes(self):
        # bid at 0.49, mid 0.50 → 1c inside a 4.5c band
        score, notional = inband_score(
            [{"price": 0.49, "size": 100}], 0.50, 4.5, True)
        assert score > 0
        assert notional == pytest.approx(100 * 0.49)

    def test_out_of_band_skipped(self):
        score, notional = inband_score(
            [{"price": 0.40, "size": 100}], 0.50, 1.0, True)  # 10c >> 1c band
        assert score == 0
        assert notional == 0

    def test_ask_side_notional(self):
        score, notional = inband_score(
            [{"price": 0.51, "size": 100}], 0.50, 4.5, False)
        assert notional == pytest.approx(100 * (1 - 0.51))

    def test_zero_max_spread(self):
        score, _ = inband_score([{"price": 0.50, "size": 100}], 0.50, 0.0, True)
        assert score == 0

    def test_bad_level_skipped(self):
        score, notional = inband_score(
            [{"price": "x", "size": 100}, {"price": 0.49, "size": 50}],
            0.50, 4.5, True)
        assert notional == pytest.approx(50 * 0.49)


# ---------------------------------------------------------------------------
# reward_share
# ---------------------------------------------------------------------------

class TestRewardShare:
    def test_empty_band_full_share(self):
        assert reward_share(50, 0.01, 4.5, 0.0) == pytest.approx(1.0)

    def test_contested_partial_share(self):
        s = reward_share(50, 0.01, 4.5, 1000.0)
        assert 0 < s < 1

    def test_zero_max_spread(self):
        assert reward_share(50, 0.01, 0.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# classify_jump_risk
# ---------------------------------------------------------------------------

class TestClassifyJumpRisk:
    def test_no_history(self):
        out = classify_jump_risk([0.5] * 3, 10.0, 50)
        assert out["verdict"] == "no-history"

    def test_safe(self):
        prices = [0.50 + 0.001 * (i % 2) for i in range(20)]  # tiny moves
        out = classify_jump_risk(prices, 100.0, 50)
        assert out["verdict"] == "SAFE"
        assert out["days_wiped"] is not None

    def test_kill_big_jump(self):
        prices = [0.10] * 10 + [0.90] + [0.90] * 9  # one 80c jump
        out = classify_jump_risk(prices, 1.0, 50)
        assert out["verdict"] == "KILL"

    def test_watch_band(self):
        # tune so days_wiped lands in (7, 20]
        prices = [0.50] * 10 + [0.60] + [0.60] * 9  # one 10c jump
        # jump_loss = 50 * 0.10 = 5.0 ; reward/day = 0.5 → 10 days wiped → WATCH
        out = classify_jump_risk(prices, 0.5, 50)
        assert out["verdict"] == "WATCH"

    def test_zero_reward_is_kill_with_none_days(self):
        prices = [0.50 + 0.001 * (i % 2) for i in range(20)]
        out = classify_jump_risk(prices, 0.0, 50)
        assert out["verdict"] == "KILL"
        assert out["days_wiped"] is None


# ---------------------------------------------------------------------------
# _best
# ---------------------------------------------------------------------------

class TestBest:
    def test_empty(self):
        assert _best([], is_bid=True) is None

    def test_bid_max(self):
        assert _best([{"price": 0.4}, {"price": 0.49}], is_bid=True) == 0.49

    def test_ask_min(self):
        assert _best([{"price": 0.6}, {"price": 0.51}], is_bid=False) == 0.51

    def test_bad_level_skipped(self):
        assert _best([{"price": "x"}, {"price": 0.5}], is_bid=True) == 0.5


# ---------------------------------------------------------------------------
# score_pool
# ---------------------------------------------------------------------------

class TestScorePool:
    def _pool(self, **kw):
        return parse_rewards(_market(**kw))

    def test_one_sided_book_is_none(self):
        pool = self._pool()
        assert score_pool(pool, {"bids": [], "asks": [{"price": 0.5, "size": 1}]}, []) is None

    def test_success_contested(self):
        pool = self._pool(daily=100.0, max_spread=4.5, min_size=50.0)
        book = {
            "bids": [{"price": 0.49, "size": 5000}],
            "asks": [{"price": 0.51, "size": 5000}],
        }
        hist = [{"p": 0.50 + 0.001 * (i % 2)} for i in range(20)]
        row = score_pool(pool, book, hist)
        assert 0 < row["share"] < 1
        assert row["empty_band"] is False
        assert row["jump_verdict"] == "SAFE"
        assert row["reward_per_day"] > 0

    def test_empty_band_flagged(self):
        pool = self._pool(max_spread=2.0)
        book = {  # bid/ask far outside the 2c band → empty in-band → share 1.0
            "bids": [{"price": 0.10, "size": 100}],
            "asks": [{"price": 0.90, "size": 100}],
        }
        row = score_pool(pool, book, [])
        assert row["empty_band"] is True
        assert row["jump_verdict"] == "no-history"

    def test_history_bad_point_skipped(self):
        pool = self._pool()
        book = {"bids": [{"price": 0.49, "size": 100}],
                "asks": [{"price": 0.51, "size": 100}]}
        hist = [{"p": "bad"}] + [{"p": 0.5}] * 12
        row = score_pool(pool, book, hist)
        assert row["jump_verdict"] in ("SAFE", "WATCH", "KILL")


# ---------------------------------------------------------------------------
# RewardsClient (HTTP) — pytest-httpx
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    c = RewardsClient()
    yield c
    c.close()


class TestRewardsClientHTTP:
    def test_sampling_markets_dict_wrapped(self, client, httpx_mock):
        httpx_mock.add_response(
            url=CLOB_BASE + "/sampling-markets", json={"data": [_market()]})
        rows = client.sampling_markets()
        assert rows[0]["question"] == "Q?"

    def test_sampling_markets_bare_list(self, client, httpx_mock):
        httpx_mock.add_response(url=CLOB_BASE + "/sampling-markets", json=[_market()])
        assert len(client.sampling_markets()) == 1

    def test_sampling_markets_non_list(self, client, httpx_mock):
        httpx_mock.add_response(url=CLOB_BASE + "/sampling-markets", json={"data": "x"})
        assert client.sampling_markets() == []

    def test_sampling_markets_paginates(self, client, httpx_mock):
        httpx_mock.add_response(json={"data": [_market()], "next_cursor": "ABC"})
        httpx_mock.add_response(json={"data": [_market()], "next_cursor": "LTE="})
        rows = client.sampling_markets()
        assert len(rows) == 2
        assert len(httpx_mock.get_requests()) == 2

    def test_book_success(self, client, httpx_mock):
        httpx_mock.add_response(json={"bids": [{"price": 0.5, "size": 1}], "asks": []})
        book = client.book(TOKEN)
        assert book["bids"][0]["price"] == 0.5

    def test_book_non_dict(self, client, httpx_mock):
        httpx_mock.add_response(json=[])
        assert client.book(TOKEN) == {}

    def test_prices_history_success(self, client, httpx_mock):
        httpx_mock.add_response(json={"history": [{"t": 1, "p": 0.5}]})
        hist = client.prices_history(TOKEN)
        assert hist[0]["p"] == 0.5

    def test_prices_history_dict_no_history(self, client, httpx_mock):
        httpx_mock.add_response(json={"foo": 1})
        assert client.prices_history(TOKEN) == []

    def test_prices_history_non_dict(self, client, httpx_mock):
        httpx_mock.add_response(json=[1, 2])
        assert client.prices_history(TOKEN) == []

    def test_http_status_error(self, client, httpx_mock):
        httpx_mock.add_response(status_code=502, text="boom")
        with pytest.raises(ApiError) as exc:
            client.sampling_markets()
        assert exc.value.status_code == 502

    def test_request_error(self, client, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("down"))
        with pytest.raises(ApiError):
            client.sampling_markets()

    def test_injected_http_client(self):
        c = RewardsClient(http=httpx.Client())
        c.close()


# ---------------------------------------------------------------------------
# scan / run_scan — FakeClient
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, markets, books, histories=None, book_errors=None,
                 history_errors=None):
        self.markets = markets
        self.books = books
        self.histories = histories or {}
        self.book_errors = book_errors or set()
        self.history_errors = history_errors or set()
        self.closed = False

    def sampling_markets(self):
        return self.markets

    def book(self, token_id):
        if token_id in self.book_errors:
            raise ApiError("book down")
        return self.books.get(token_id, {})

    def prices_history(self, token_id, *, interval="max", fidelity=1440):
        if token_id in self.history_errors:
            raise ApiError("history down")
        return self.histories.get(token_id, [])

    def close(self):
        self.closed = True


def _two_sided(bid=0.49, ask=0.51, size=5000):
    return {"bids": [{"price": bid, "size": size}],
            "asks": [{"price": ask, "size": size}]}


class TestScan:
    def test_filters_below_min_daily(self):
        markets = [_market(daily=10.0, token="lo"), _market(daily=100.0, token="hi")]
        fc = FakeClient(markets, {"hi": _two_sided()},
                        histories={"hi": [{"p": 0.5} for _ in range(15)]})
        report = rewards.scan(fc, min_daily=50.0)
        qs = [p for p in report["pools"]]
        assert report["pools_scored"] == 1
        assert report["total_reward_pools"] == 2

    def test_skips_book_error_and_one_sided(self):
        markets = [
            _market(daily=100.0, token="err"),
            _market(daily=90.0, token="onesided"),
            _market(daily=80.0, token="ok"),
        ]
        books = {
            "onesided": {"bids": [], "asks": [{"price": 0.5, "size": 1}]},
            "ok": _two_sided(),
        }
        fc = FakeClient(markets, books,
                        histories={"ok": [{"p": 0.5} for _ in range(15)]},
                        book_errors={"err"})
        report = rewards.scan(fc, min_daily=50.0)
        assert report["pools_scored"] == 1

    def test_history_error_falls_back(self):
        markets = [_market(daily=100.0, token="ok")]
        fc = FakeClient(markets, {"ok": _two_sided()}, history_errors={"ok"})
        report = rewards.scan(fc, min_daily=50.0)
        assert report["pools"][0]["jump_verdict"] == "no-history"

    def test_without_jump_risk(self):
        markets = [_market(daily=100.0, token="ok")]
        fc = FakeClient(markets, {"ok": _two_sided()})
        report = rewards.scan(fc, min_daily=50.0, with_jump_risk=False)
        assert report["pools"][0]["jump_verdict"] == "no-history"

    def test_ranking_safe_first(self):
        markets = [_market(daily=100.0, token="safe"), _market(daily=400.0, token="empty")]
        books = {
            "safe": _two_sided(),
            "empty": {"bids": [{"price": 0.1, "size": 1}],
                      "asks": [{"price": 0.9, "size": 1}]},  # empty band
        }
        hist = {"safe": [{"p": 0.5 + 0.001 * (i % 2)} for i in range(20)]}
        fc = FakeClient(markets, books, histories=hist)
        report = rewards.scan(fc, min_daily=50.0)
        # empty-band pool de-prioritised despite bigger daily
        assert report["pools"][0]["question"] == "Q?"
        assert report["pools"][0]["empty_band"] is False
        assert report["safe_count"] == 1

    def test_top_cap(self):
        markets = [_market(daily=100.0 + i, token=f"t{i}") for i in range(5)]
        books = {f"t{i}": _two_sided() for i in range(5)}
        fc = FakeClient(markets, books)
        report = rewards.scan(fc, min_daily=50.0, top=2)
        assert report["pools_scored"] == 2


def test_run_scan_builds_and_closes_client(monkeypatch):
    markets = [_market(daily=100.0, token="ok")]
    fake = FakeClient(markets, {"ok": _two_sided()})
    monkeypatch.setattr(rewards, "RewardsClient", lambda: fake)
    report = rewards.run_scan(min_daily=50.0, with_jump_risk=False)
    assert report["pools_scored"] == 1
    assert fake.closed is True
