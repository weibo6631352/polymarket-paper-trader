"""Tests for the smart-money / copy-trading scanner."""

from __future__ import annotations

import httpx
import pytest

from pm_trader import smartmoney
from pm_trader.smartmoney import (
    DATA_BASE,
    LB_BASE,
    SmartMoneyClient,
    _entry_name,
    classify_position,
    parse_windows,
    rank_traders,
    validate_wallet,
)
from pm_trader.models import ApiError

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
WALLET_C = "0x" + "c" * 40


# ---------------------------------------------------------------------------
# validate_wallet
# ---------------------------------------------------------------------------

class TestValidateWallet:
    def test_valid(self):
        assert validate_wallet(WALLET_A) == WALLET_A

    @pytest.mark.parametrize("bad", [
        "0xABC",                       # too short
        "abc",                         # no 0x
        "0x" + "g" * 40,               # non-hex
        "0x" + "a" * 41,               # too long
        "",
    ])
    def test_invalid_format(self, bad):
        with pytest.raises(ValueError):
            validate_wallet(bad)

    def test_non_string(self):
        with pytest.raises(ValueError):
            validate_wallet(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_windows
# ---------------------------------------------------------------------------

class TestParseWindows:
    def test_basic(self):
        assert parse_windows("7d,30d,all") == ("7d", "30d", "all")

    def test_whitespace_and_dedup(self):
        assert parse_windows(" 7d , 7d ,30d") == ("7d", "30d")

    def test_single(self):
        assert parse_windows("all") == ("all",)

    def test_invalid_window(self):
        with pytest.raises(ValueError):
            parse_windows("7d,month")

    def test_empty(self):
        with pytest.raises(ValueError):
            parse_windows("  ,  ")


# ---------------------------------------------------------------------------
# SmartMoneyClient (HTTP) — uses pytest-httpx
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    c = SmartMoneyClient()
    yield c
    c.close()


class TestLeaderboard:
    def test_success(self, client, httpx_mock):
        httpx_mock.add_response(
            url=httpx.URL(LB_BASE + "/profit", params={"window": "30d", "limit": 5}),
            json=[{"proxyWallet": WALLET_A, "name": "alpha", "amount": 100.0}],
        )
        rows = client.leaderboard(window="30d", metric="profit", limit=5)
        assert rows[0]["name"] == "alpha"

    def test_volume_metric(self, client, httpx_mock):
        httpx_mock.add_response(
            url=httpx.URL(LB_BASE + "/volume", params={"window": "all", "limit": 3}),
            json=[{"proxyWallet": WALLET_B, "name": "vol", "amount": 9.0}],
        )
        rows = client.leaderboard(window="all", metric="volume", limit=3)
        assert rows[0]["proxyWallet"] == WALLET_B

    def test_invalid_window(self, client):
        with pytest.raises(ValueError):
            client.leaderboard(window="month")

    def test_invalid_metric(self, client):
        with pytest.raises(ValueError):
            client.leaderboard(metric="sharpe")

    def test_limit_clamped(self, client, httpx_mock):
        httpx_mock.add_response(json=[])
        client.leaderboard(window="7d", limit=999)
        req = httpx_mock.get_requests()[0]
        assert req.url.params["limit"] == "100"

    def test_limit_floor(self, client, httpx_mock):
        httpx_mock.add_response(json=[])
        client.leaderboard(window="7d", limit=0)
        req = httpx_mock.get_requests()[0]
        assert req.url.params["limit"] == "1"

    def test_non_list_response(self, client, httpx_mock):
        httpx_mock.add_response(json={"error": "nope"})
        assert client.leaderboard(window="7d") == []


class TestPositions:
    def test_success(self, client, httpx_mock):
        httpx_mock.add_response(
            json=[{"title": "T", "outcome": "No", "avgPrice": 0.3, "curPrice": 0.31}],
        )
        rows = client.positions(WALLET_A, limit=5)
        assert rows[0]["outcome"] == "No"

    def test_bad_wallet(self, client):
        with pytest.raises(ValueError):
            client.positions("not-a-wallet")

    def test_non_list_response(self, client, httpx_mock):
        httpx_mock.add_response(json={})
        assert client.positions(WALLET_A) == []


class TestTrades:
    def test_success(self, client, httpx_mock):
        httpx_mock.add_response(
            url=httpx.URL(DATA_BASE + "/trades", params={"user": WALLET_A, "limit": 5}),
            json=[
                {
                    "proxyWallet": WALLET_A,
                    "side": "BUY",
                    "asset": "abc",
                    "conditionId": "0xcond",
                    "size": 10.0,
                    "price": 0.42,
                    "timestamp": 1700000000,
                    "title": "T",
                    "slug": "t",
                    "outcome": "Yes",
                    "transactionHash": "0xhash",
                }
            ],
        )
        rows = client.trades(WALLET_A, limit=5)
        assert rows[0]["side"] == "BUY"
        assert rows[0]["timestamp"] == 1700000000

    def test_bad_wallet(self, client):
        with pytest.raises(ValueError):
            client.trades("not-a-wallet")

    def test_limit_clamped(self, client, httpx_mock):
        httpx_mock.add_response(json=[])
        client.trades(WALLET_A, limit=999)
        req = httpx_mock.get_requests()[0]
        assert req.url.params["limit"] == "100"

    def test_limit_floor(self, client, httpx_mock):
        httpx_mock.add_response(json=[])
        client.trades(WALLET_A, limit=0)
        req = httpx_mock.get_requests()[0]
        assert req.url.params["limit"] == "1"

    def test_non_list_response(self, client, httpx_mock):
        httpx_mock.add_response(json={})
        assert client.trades(WALLET_A) == []


class TestValue:
    def test_success(self, client, httpx_mock):
        httpx_mock.add_response(
            url=httpx.URL(DATA_BASE + "/value", params={"user": WALLET_A}),
            json=[{"user": WALLET_A, "value": 1234.5}],
        )
        assert client.value(WALLET_A) == 1234.5

    def test_empty_list(self, client, httpx_mock):
        httpx_mock.add_response(json=[])
        assert client.value(WALLET_A) == 0.0

    def test_non_list(self, client, httpx_mock):
        httpx_mock.add_response(json={"value": 5})
        assert client.value(WALLET_A) == 0.0

    def test_null_value(self, client, httpx_mock):
        httpx_mock.add_response(json=[{"value": None}])
        assert client.value(WALLET_A) == 0.0

    def test_malformed_value(self, client, httpx_mock):
        httpx_mock.add_response(json=[{"value": "abc"}])
        assert client.value(WALLET_A) == 0.0

    def test_non_dict_element(self, client, httpx_mock):
        httpx_mock.add_response(json=[123])
        assert client.value(WALLET_A) == 0.0

    def test_bad_wallet(self, client):
        with pytest.raises(ValueError):
            client.value("nope")


class TestClientErrors:
    def test_http_status_error(self, client, httpx_mock):
        httpx_mock.add_response(status_code=500, text="boom")
        with pytest.raises(ApiError) as exc:
            client.leaderboard(window="7d")
        assert exc.value.status_code == 500

    def test_request_error(self, client, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("down"))
        with pytest.raises(ApiError):
            client.leaderboard(window="7d")

    def test_injected_http_client(self):
        # A caller-provided client is used as-is and closed cleanly.
        inner = httpx.Client()
        c = SmartMoneyClient(http=inner)
        c.close()


# ---------------------------------------------------------------------------
# _entry_name
# ---------------------------------------------------------------------------

class TestEntryName:
    def test_name(self):
        assert _entry_name({"name": "Theo4", "proxyWallet": WALLET_A}) == "Theo4"

    def test_pseudonym_fallback(self):
        assert _entry_name({"pseudonym": "ghost", "proxyWallet": WALLET_A}) == "ghost"

    def test_wallet_like_name_anonymised(self):
        out = _entry_name({"name": "0xC41D-177710", "proxyWallet": WALLET_A})
        assert out.startswith("0xaaaa") and "…" in out

    def test_no_name_no_wallet(self):
        assert _entry_name({}) == "unknown"

    def test_empty_name_uses_wallet(self):
        out = _entry_name({"name": "", "proxyWallet": WALLET_B})
        assert out == f"{WALLET_B[:6]}…{WALLET_B[-4:]}"


# ---------------------------------------------------------------------------
# rank_traders
# ---------------------------------------------------------------------------

class TestRankTraders:
    def test_consistency_ordering(self):
        per = {
            "7d": [{"proxyWallet": WALLET_A, "name": "A", "amount": 9},
                   {"proxyWallet": WALLET_B, "name": "B", "amount": 8}],
            "30d": [{"proxyWallet": WALLET_A, "name": "A", "amount": 9}],
            "all": [{"proxyWallet": WALLET_A, "name": "A", "amount": 9}],
        }
        ranked = rank_traders(per)
        # A appears in all 3 windows → ranked first
        assert ranked[0]["wallet"] == WALLET_A
        assert len(ranked[0]["windows"]) == 3
        assert ranked[1]["wallet"] == WALLET_B
        assert ranked[0]["best_rank"] == 1

    def test_best_rank_tracks_minimum(self):
        per = {
            "7d": [{"proxyWallet": WALLET_A}, {"proxyWallet": WALLET_B}],
            "30d": [{"proxyWallet": WALLET_B}, {"proxyWallet": WALLET_A}],
        }
        ranked = rank_traders(per)
        by_wallet = {t["wallet"]: t for t in ranked}
        assert by_wallet[WALLET_A]["best_rank"] == 1
        assert by_wallet[WALLET_B]["best_rank"] == 1

    def test_missing_wallet_skipped(self):
        per = {"7d": [{"name": "nowallet"}, {"proxyWallet": WALLET_A}]}
        ranked = rank_traders(per)
        assert len(ranked) == 1
        assert ranked[0]["wallet"] == WALLET_A


# ---------------------------------------------------------------------------
# classify_position
# ---------------------------------------------------------------------------

class TestClassifyPosition:
    def test_fresh(self):
        assert classify_position({"avgPrice": 0.29, "curPrice": 0.305}) == "fresh"

    def test_moved(self):
        assert classify_position({"avgPrice": 0.65, "curPrice": 0.82}) == "moved"

    def test_underwater(self):
        assert classify_position({"avgPrice": 0.50, "curPrice": 0.40}) == "underwater"

    def test_resolving_high(self):
        assert classify_position({"avgPrice": 0.15, "curPrice": 0.99}) == "resolving"

    def test_resolving_low(self):
        assert classify_position({"avgPrice": 0.40, "curPrice": 0.02}) == "resolving"

    def test_missing_prices_default_resolving(self):
        # No prices → cur 0.0 ≤ min_price → resolving
        assert classify_position({}) == "resolving"


# ---------------------------------------------------------------------------
# scan / run_scan — FakeClient
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, boards=None, values=None, positions=None):
        self.boards = boards or {}
        self.values = values or {}
        self.positions_map = positions or {}
        self.closed = False

    def leaderboard(self, *, window, metric="profit", limit=50):
        return self.boards.get((window, metric), [])

    def value(self, wallet):
        return self.values.get(wallet, 0.0)

    def positions(self, wallet, *, limit=20):
        return self.positions_map.get(wallet, [])

    def close(self):
        self.closed = True


def _scenario():
    boards = {
        ("7d", "profit"): [
            {"proxyWallet": WALLET_A, "name": "A", "amount": 9_000_000},
            {"proxyWallet": WALLET_B, "name": "B", "amount": 1_000_000},
        ],
        ("30d", "profit"): [
            {"proxyWallet": WALLET_A, "name": "A", "amount": 9_000_000},
            {"proxyWallet": WALLET_B, "name": "B", "amount": 1_000_000},
            {"proxyWallet": WALLET_C, "name": "C", "amount": 500_000},
        ],
        ("all", "profit"): [
            {"proxyWallet": WALLET_A, "name": "A", "amount": 9_000_000},
        ],
    }
    values = {WALLET_A: 90_000.0, WALLET_B: 2_000.0, WALLET_C: 0.0}
    positions = {
        WALLET_A: [
            # fresh, big → candidate
            {"title": "Austria win?", "slug": "aut", "conditionId": "0x1",
             "outcome": "No", "avgPrice": 0.29, "curPrice": 0.305,
             "currentValue": 70_000.0, "endDate": "2026-06-17"},
            # moved → filtered
            {"title": "Argentina win?", "slug": "arg", "outcome": "Yes",
             "avgPrice": 0.65, "curPrice": 0.82, "currentValue": 1_500_000.0},
            # resolving → filtered
            {"title": "Iran peace", "slug": "irn", "outcome": "Yes",
             "avgPrice": 0.15, "curPrice": 0.99, "currentValue": 900_000.0},
            # dust fresh → filtered by min_position_value
            {"title": "tiny", "slug": "tin", "outcome": "No",
             "avgPrice": 0.30, "curPrice": 0.30, "currentValue": 100.0},
        ],
        WALLET_B: [
            # underwater → candidate (cheaper than their entry)
            {"title": "Tigers ML", "slug": "det", "outcome": "Detroit",
             "avgPrice": 0.50, "curPrice": 0.40, "currentValue": 2_000.0},
        ],
        WALLET_C: [],
    }
    return boards, values, positions


class TestScan:
    def test_candidates_filtered_and_sorted(self):
        boards, values, positions = _scenario()
        fake = FakeClient(boards, values, positions)
        report = smartmoney.scan(
            fake, windows=("7d", "30d", "all"), top_traders=10,
            min_position_value=500.0,
        )
        cands = report["candidates"]
        # Only the fresh (A) and underwater (B) survive
        assert len(cands) == 2
        assert report["candidate_count"] == 2
        # A is in all 3 windows → sorted first
        assert cands[0]["trader"] == "A"
        assert cands[0]["class"] == "fresh"
        assert cands[0]["window_count"] == 3
        assert cands[0]["drift"] == pytest.approx(0.015)
        assert cands[1]["trader"] == "B"
        assert cands[1]["class"] == "underwater"

    def test_trader_summary(self):
        boards, values, positions = _scenario()
        fake = FakeClient(boards, values, positions)
        report = smartmoney.scan(fake, windows=("7d", "30d", "all"))
        traders = {t["name"]: t for t in report["traders"]}
        assert traders["A"]["open_value"] == 90_000.0
        assert traders["A"]["copyable_count"] == 1
        assert traders["C"]["copyable_count"] == 0
        assert report["params"]["metric"] == "profit"

    def test_top_traders_limit(self):
        boards, values, positions = _scenario()
        fake = FakeClient(boards, values, positions)
        report = smartmoney.scan(fake, windows=("7d", "30d", "all"), top_traders=1)
        assert report["traders_scanned"] == 1
        # Only trader A scanned → B's underwater not present
        assert all(c["trader"] == "A" for c in report["candidates"])


def test_run_scan_builds_and_closes_client(monkeypatch):
    boards, values, positions = _scenario()
    fake = FakeClient(boards, values, positions)
    monkeypatch.setattr(smartmoney, "SmartMoneyClient", lambda: fake)
    report = smartmoney.run_scan(windows=("7d", "30d", "all"), top_traders=5)
    assert fake.closed is True
    assert report["candidate_count"] == 2
