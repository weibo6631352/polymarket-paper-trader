"""Smart-money scanner — surface Polymarket's consistently profitable traders
and their fresh, copyable open positions.

Two public surfaces:
  - ``SmartMoneyClient``: a thin HTTP client for Polymarket's leaderboard API
    (``lb-api``) and data API (``data-api``).  These are PUBLIC endpoints,
    separate from Gamma/CLOB, and need no auth.
  - ``run_scan`` / ``scan``: orchestrate a multi-window leaderboard pull, rank
    traders by cross-window consistency (skill, not variance), then filter each
    trader's open positions down to the fresh/copyable subset.

The scanner deliberately does NOT decide whether to copy.  It surfaces
high-signal candidates (a consistent trader holding a fresh, liquid position)
for a downstream evaluator to de-vig and Kelly-size.  A leaderboard name is not
an edge by itself: most leaders are flat (profit already realized) or hold
positions that already ran past their entry — this module filters those out.
"""

from __future__ import annotations

import re

import httpx

from pm_trader.models import ApiError

LB_BASE = "https://lb-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"

VALID_WINDOWS = ("1d", "7d", "30d", "all")
VALID_METRICS = ("profit", "volume")

_TIMEOUT = httpx.Timeout(15.0)
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Position classification thresholds (overridable per scan)
DRIFT_TOL = 0.07   # |cur - trader_avg| within this → still a comparable entry
MIN_PRICE = 0.05   # at/under this the outcome is ~resolved (no edge left)
MAX_PRICE = 0.95   # at/over this the edge is realized / market ~settled

# Positions worth copying still enter at-or-below the trader's average price.
_COPYABLE = ("fresh", "underwater")


def validate_wallet(wallet: str) -> str:
    """Validate a 0x-prefixed 40-hex-char wallet address.

    Guards the data-api ``user`` parameter against injection / SSRF.
    """
    if not isinstance(wallet, str) or not _WALLET_RE.match(wallet):
        raise ValueError(f"Invalid wallet address: {wallet!r}")
    return wallet


def parse_windows(spec: str) -> tuple[str, ...]:
    """Parse a comma-separated window spec into a validated, de-duped tuple."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in spec.split(","):
        w = raw.strip()
        if not w:
            continue
        if w not in VALID_WINDOWS:
            raise ValueError(
                f"Invalid window {w!r}; use one of {', '.join(VALID_WINDOWS)}"
            )
        if w not in seen:
            seen.add(w)
            out.append(w)
    if not out:
        raise ValueError("No valid windows provided")
    return tuple(out)


class SmartMoneyClient:
    """HTTP client for Polymarket's public leaderboard + data APIs."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http if http is not None else httpx.Client(timeout=_TIMEOUT)

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict | None = None) -> list | dict:
        try:
            resp = self._http.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise ApiError(
                f"Polymarket data API error: {e.response.status_code} "
                f"{e.response.text[:200]}",
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise ApiError(f"Polymarket data API request failed: {e}") from e

    def leaderboard(
        self, *, window: str = "30d", metric: str = "profit", limit: int = 50
    ) -> list[dict]:
        """Fetch the top traders for one window/metric (profit or volume)."""
        if window not in VALID_WINDOWS:
            raise ValueError(
                f"Invalid window {window!r}; use one of {', '.join(VALID_WINDOWS)}"
            )
        if metric not in VALID_METRICS:
            raise ValueError(
                f"Invalid metric {metric!r}; use one of {', '.join(VALID_METRICS)}"
            )
        limit = max(1, min(int(limit), 100))
        data = self._get(
            f"{LB_BASE}/{metric}", params={"window": window, "limit": limit}
        )
        return data if isinstance(data, list) else []

    def positions(self, wallet: str, *, limit: int = 20) -> list[dict]:
        """Fetch a trader's current open positions, biggest first."""
        wallet = validate_wallet(wallet)
        limit = max(1, min(int(limit), 100))
        data = self._get(
            f"{DATA_BASE}/positions",
            params={
                "user": wallet,
                "limit": limit,
                "sortBy": "CURRENT",
                "sortDirection": "DESC",
            },
        )
        return data if isinstance(data, list) else []

    def trades(self, wallet: str, *, limit: int = 100) -> list[dict]:
        """Fetch a trader's recent fill history, most recent first.

        Returns per-fill records ``{proxyWallet, side, asset, conditionId, size,
        price, timestamp, title, slug, outcome, transactionHash}``.  The
        ``timestamp`` is whole-second Unix epoch — Polymarket batch-matches, so
        many fills share one second; do not infer sub-second timing from it.
        """
        wallet = validate_wallet(wallet)
        limit = max(1, min(int(limit), 100))
        data = self._get(
            f"{DATA_BASE}/trades", params={"user": wallet, "limit": limit}
        )
        return data if isinstance(data, list) else []

    def value(self, wallet: str) -> float:
        """Fetch a trader's total open-position value (0 if flat)."""
        wallet = validate_wallet(wallet)
        data = self._get(f"{DATA_BASE}/value", params={"user": wallet})
        if isinstance(data, list) and data:
            try:
                return float(data[0].get("value", 0.0) or 0.0)
            except (TypeError, ValueError, AttributeError):
                return 0.0
        return 0.0


def _entry_name(entry: dict) -> str:
    """Human label for a leaderboard entry; anonymise wallet-like names."""
    name = (entry.get("name") or entry.get("pseudonym") or "").strip()
    wallet = entry.get("proxyWallet", "") or ""
    if name and not name.startswith("0x"):
        return name
    if wallet:
        return f"{wallet[:6]}…{wallet[-4:]}"
    return name or "unknown"


def rank_traders(per_window: dict[str, list[dict]]) -> list[dict]:
    """Merge per-window leaderboards into a consistency-ranked trader list.

    A trader present in more windows is more likely skilled than lucky; ties
    are broken by the best (lowest) rank achieved across windows.
    """
    traders: dict[str, dict] = {}
    for window, entries in per_window.items():
        for rank, entry in enumerate(entries, start=1):
            wallet = entry.get("proxyWallet")
            if not wallet:
                continue
            t = traders.setdefault(
                wallet,
                {
                    "wallet": wallet,
                    "name": _entry_name(entry),
                    "windows": {},
                    "best_rank": rank,
                },
            )
            t["windows"][window] = {
                "rank": rank,
                "amount": entry.get("amount", 0.0),
            }
            t["best_rank"] = min(t["best_rank"], rank)
    ranked = list(traders.values())
    ranked.sort(key=lambda t: (-len(t["windows"]), t["best_rank"]))
    return ranked


def classify_position(
    pos: dict,
    *,
    drift_tol: float = DRIFT_TOL,
    min_price: float = MIN_PRICE,
    max_price: float = MAX_PRICE,
) -> str:
    """Classify a position by how copyable its current price is.

    Returns one of:
      - ``"resolving"`` — price near 0 or 1; market ~settled, no edge left.
      - ``"moved"``     — price ran up past the trader's entry; too late to copy.
      - ``"underwater"``— price below the trader's entry; cheaper entry than they
        got (could be value, could be a falling knife — evaluate carefully).
      - ``"fresh"``     — price ≈ the trader's average; a clean copyable entry.
    """
    avg = float(pos.get("avgPrice") or 0.0)
    cur = float(pos.get("curPrice") or 0.0)
    if cur <= min_price or cur >= max_price:
        return "resolving"
    drift = cur - avg
    if drift > drift_tol:
        return "moved"
    if drift < -drift_tol:
        return "underwater"
    return "fresh"


def scan(
    client: SmartMoneyClient,
    *,
    windows: tuple[str, ...] = ("7d", "30d", "all"),
    metric: str = "profit",
    per_window: int = 50,
    top_traders: int = 15,
    position_limit: int = 10,
    min_position_value: float = 500.0,
    drift_tol: float = DRIFT_TOL,
    min_price: float = MIN_PRICE,
    max_price: float = MAX_PRICE,
) -> dict:
    """Scan the leaderboard and return a structured smart-money report.

    Pulls each window's leaderboard, ranks traders by cross-window consistency,
    then for the top ``top_traders`` collects their open positions and keeps the
    fresh/copyable, above-dust subset.  Candidates are sorted by consistency
    (window count) then position size.
    """
    per: dict[str, list[dict]] = {}
    for w in windows:
        per[w] = client.leaderboard(window=w, metric=metric, limit=per_window)

    ranked = rank_traders(per)
    selected = ranked[: max(1, top_traders)]

    traders_out: list[dict] = []
    candidates: list[dict] = []
    for t in selected:
        wallet = t["wallet"]
        open_value = client.value(wallet)
        positions = client.positions(wallet, limit=position_limit)
        windows_present = sorted(t["windows"].keys())
        copyable: list[dict] = []
        for pos in positions:
            cur_value = float(pos.get("currentValue") or 0.0)
            if cur_value < min_position_value:
                continue
            cls = classify_position(
                pos, drift_tol=drift_tol, min_price=min_price, max_price=max_price
            )
            if cls not in _COPYABLE:
                continue
            avg = float(pos.get("avgPrice") or 0.0)
            cur = float(pos.get("curPrice") or 0.0)
            copyable.append(
                {
                    "trader": t["name"],
                    "wallet": wallet,
                    "windows": windows_present,
                    "window_count": len(windows_present),
                    "title": pos.get("title", ""),
                    "slug": pos.get("slug", ""),
                    "condition_id": pos.get("conditionId", ""),
                    "outcome": pos.get("outcome", ""),
                    "trader_avg": round(avg, 4),
                    "cur_price": round(cur, 4),
                    "drift": round(cur - avg, 4),
                    "position_value": round(cur_value, 2),
                    "class": cls,
                    "end_date": pos.get("endDate", ""),
                }
            )
        traders_out.append(
            {
                "name": t["name"],
                "wallet": wallet,
                "windows": windows_present,
                "window_count": len(windows_present),
                "best_rank": t["best_rank"],
                "open_value": round(open_value, 2),
                "copyable_count": len(copyable),
            }
        )
        candidates.extend(copyable)

    candidates.sort(key=lambda c: (-c["window_count"], -c["position_value"]))
    return {
        "params": {
            "windows": list(windows),
            "metric": metric,
            "per_window": per_window,
            "top_traders": top_traders,
            "min_position_value": min_position_value,
            "drift_tol": drift_tol,
        },
        "traders_scanned": len(selected),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "traders": traders_out,
    }


def run_scan(**kwargs: object) -> dict:
    """Convenience wrapper: build a client, scan, and always close the client."""
    client = SmartMoneyClient()
    try:
        return scan(client, **kwargs)  # type: ignore[arg-type]
    finally:
        client.close()
