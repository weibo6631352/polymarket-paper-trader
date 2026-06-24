"""Portfolio orchestrator — turn the pool scanner + single-pool bot into a system.

The validated edge is capacity-constrained: each two-sided ``min_size`` quote earns
a few $/day but locks only ~$50-100, so the strategy is "spread a small book across
many uncorrelated SAFE mid-tail pools."  This module does exactly that:

  scan → select fundable SAFE pools within a capital budget → one dry-run maker bot
  per pool → aggregate expected reward, committed capital, and per-poll plans.

Dry-run only (no real orders); composes ``scanner`` + ``live`` + ``reward_math``.
"""

from __future__ import annotations

import re

from pm_trader.maker_live import LiveMakerBot
from pm_trader.orderbook import committed_capital
from pm_trader.rewards import RewardsClient, scan

DEFAULT_CAPITAL = 1000.0

# Generic words that carry no correlation signal (so two markets sharing only
# these are NOT treated as the same event).
_STOPWORDS = frozenset(
    "will the be in on at a an of to and or by win wins won most next be the for "
    "vs end up down above below before after into out as is are reach hit with "
    "who what when which than then over under between".split()
)


def _significant_tokens(question: str) -> set[str]:
    """Distinctive lowercase tokens of a question (entities/places), minus
    stopwords, pure numbers, and very short tokens — used to detect correlation."""
    toks = re.findall(r"[a-z0-9]+", (question or "").lower())
    return {t for t in toks if len(t) > 2 and not t.isdigit() and t not in _STOPWORDS}


# Curated topic/entity clusters: markets matching the same cluster move together
# (same FOMC, same country's politics, same race) even when they share no surface
# words — e.g. "Grindeanu next PM" and "Bolojan out" are both the Romanian PM.
# Surface-token overlap can't catch those, so we cluster on known correlated themes.
# Extend as new correlated themes appear in the reward universe.
_CLUSTER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("us-fed", ("fed", "fomc", "federal reserve")),
    ("romania", ("romania", "grindeanu", "bolojan", "ciolacu")),
    ("colombia", ("colombia", "petro")),
    ("israel", ("israel", "netanyahu", "knesset", "eizenkot", "bennett")),
    ("france", ("france", "french", "bardella", "macron", "philippe", "melenchon")),
    ("ecb", ("ecb", "european central bank", "lagarde")),
    ("boe", ("bank of england", "boe")),
)


def _cluster_key(question: str) -> str | None:
    """Canonical correlation cluster for a question, or None if it matches no
    known correlated theme.  Catches same-event/same-country pairs that share no
    surface words.  Heuristic + curated — it cannot catch every entity link
    (e.g. an unlisted politician's name), so it complements, not replaces, the
    token-overlap check."""
    q = (question or "").lower()
    for name, keywords in _CLUSTER_RULES:
        if any(kw in q for kw in keywords):
            return name
    return None


def _risk_adjusted_score(p: dict, risk_tolerance_days: float) -> float:
    """Reward per day, discounted by jump-tail exposure.

    ``reward_per_day`` is the steady income; ``days_wiped`` is how many days of it
    one historical max jump erases (bigger = jumpier).  The score gives full credit
    when a jump costs little relative to income and shrinks it as jump exposure
    grows — so ranking favours pools that are BOTH high-yield AND jump-safe, not
    just cheap (old min_size sort) or just high gross yield.
    """
    reward = p.get("reward_per_day")
    if reward is None:
        reward = p.get("share", 0.0) * p.get("daily", 0.0)
    days_wiped = p.get("days_wiped")
    if days_wiped is None:
        days_wiped = 9_999.0
    return reward / (1.0 + days_wiped / risk_tolerance_days)


def select_pools(
    scan_report: dict,
    *,
    capital: float = DEFAULT_CAPITAL,
    max_pools: int = 40,
    require_safe: bool = True,
    half_spread_ticks: int = 1,
    risk_tolerance_days: float = 7.0,
    max_token_overlap: int = 1,
    cooldown: set | None = None,
) -> list[dict]:
    """Pick a fundable, diversified book by RISK-ADJUSTED yield within a budget.

    Selection criteria, in order:
      0. Cooldown: skip any pool (by condition_id or token) in ``cooldown`` — a
         market that just had a catalyst jump is no longer the quiet SAFE pool we
         classified, so don't re-enter it until it has settled.
      1. Eligibility: SAFE + non-empty-band (unless ``require_safe=False``).
      2. Rank by RISK-ADJUSTED yield (``reward_per_day`` discounted by jump-tail
         exposure) — not cheapest-first, not gross-yield-only.
      3. Correlation filter: skip a candidate that is in the same curated topic
         cluster (Fed, a country's politics, …) as an already-selected pool, OR
         shares more than ``max_token_overlap`` distinctive tokens with one
         (same race / event / entity ⇒ correlated ⇒ no real diversification).
      4. Fund greedily down the ranked list until the capital budget or
         ``max_pools`` is hit.
    """
    cd = cooldown or set()
    cands = [
        p for p in scan_report.get("pools", [])
        if p.get("condition_id") not in cd and p.get("token") not in cd
        and (not require_safe or (p.get("jump_verdict") == "SAFE" and not p.get("empty_band")))
    ]
    cands.sort(key=lambda p: -_risk_adjusted_score(p, risk_tolerance_days))

    selected: list[dict] = []
    chosen_tokens: list[set[str]] = []
    chosen_clusters: set[str] = set()
    spent = 0.0
    for p in cands:
        half_spread_c = p["tick"] * 100.0 * half_spread_ticks
        cap = committed_capital(p["min_size"], half_spread_c)
        if cap <= 0 or spent + cap > capital:
            continue
        cluster = _cluster_key(p["question"])
        if cluster is not None and cluster in chosen_clusters:
            continue  # same curated topic/country cluster ⇒ correlated
        toks = _significant_tokens(p["question"])
        if toks and any(len(toks & ct) > max_token_overlap for ct in chosen_tokens):
            continue  # surface-token correlation
        selected.append({
            "question": p["question"],
            "condition_id": p["condition_id"],
            "token": p["token"],
            "daily": p["daily"],
            "share": p["share"],
            "min_size": p["min_size"],
            "tick": p["tick"],
            "max_spread_c": p["max_spread_c"],
            "half_spread_c": half_spread_c,
            "committed_capital": round(cap, 2),
            "est_daily_reward": round(p["share"] * p["daily"], 4),
            "risk_adj_score": round(_risk_adjusted_score(p, risk_tolerance_days), 4),
        })
        spent += cap
        chosen_tokens.append(toks)
        if cluster is not None:
            chosen_clusters.add(cluster)
        if len(selected) >= max_pools:
            break
    return selected


class MakerPortfolio:
    """A dry-run portfolio of two-sided maker quotes across selected pools."""

    def __init__(self, selected: list[dict], *, dry_run: bool = True) -> None:
        self.meta: dict[str, dict] = {}
        self.bots: dict[str, LiveMakerBot] = {}
        for s in selected:
            self.meta[s["token"]] = s
            self.bots[s["token"]] = LiveMakerBot(
                token_id=s["token"], max_spread_c=s["max_spread_c"],
                min_size=s["min_size"], tick=s["tick"],
                half_spread_c=s["half_spread_c"], dry_run=dry_run,
            )

    def committed_capital(self) -> float:
        return round(
            sum(committed_capital(b.size, b.half_spread_c) for b in self.bots.values()), 2
        )

    def est_daily_reward(self) -> float:
        return round(sum(m["est_daily_reward"] for m in self.meta.values()), 4)

    def plan_all(self, market_data: dict) -> list[dict]:
        """Run one quoting step per pool. ``market_data``: {token: (OrderBook, mid)}."""
        plans = []
        for token, bot in self.bots.items():
            md = market_data.get(token)
            if md is None:
                continue
            book, mid = md
            plans.append({"question": self.meta[token]["question"], **bot.step(book, mid)})
        return plans

    def summary(self) -> dict:
        cap = self.committed_capital()
        daily = self.est_daily_reward()
        return {
            "pools": len(self.bots),
            "committed_capital": cap,
            "free_implied": None,
            "est_daily_reward": daily,
            "est_annualized_pct": round(daily * 365 / cap * 100, 1) if cap > 0 else 0.0,
        }


def fetch_market_data(client: RewardsClient, selected: list[dict]) -> dict:
    """Pull a live (OrderBook, mid) for each selected pool's token, for plan_all.

    Returns {token: (OrderBook, mid)}; pools whose book is empty/one-sided or
    errors are skipped (so a single bad market doesn't sink the portfolio).
    """
    from pm_trader.models import OrderBook, OrderBookLevel

    out: dict = {}
    for s in selected:
        token = s["token"]
        try:
            raw = client.book(token)
        except Exception:
            continue
        bids = [OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
                for b in (raw.get("bids") or []) if "price" in b and "size" in b]
        asks = [OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
                for a in (raw.get("asks") or []) if "price" in a and "size" in a]
        if not bids or not asks:
            continue
        mid = (max(b.price for b in bids) + min(a.price for a in asks)) / 2.0
        out[token] = (OrderBook(bids=bids, asks=asks), mid)
    return out


def build_portfolio(
    client: RewardsClient,
    *,
    capital: float = DEFAULT_CAPITAL,
    min_daily: float = 80.0,
    top: int = 40,
    max_pools: int = 40,
) -> dict:
    """Scan live, select a fundable SAFE book, and return its plan + summary (dry-run)."""
    report = scan(client, min_daily=min_daily, top=top, with_jump_risk=True)
    selected = select_pools(report, capital=capital, max_pools=max_pools)
    portfolio = MakerPortfolio(selected, dry_run=True)
    market_data = fetch_market_data(client, selected)
    plans = portfolio.plan_all(market_data)
    return {
        "capital_budget": capital,
        "selected": selected,
        "summary": portfolio.summary(),
        "planned": len(plans),
        "plans": plans,
    }


def run(**kwargs: object) -> dict:
    """Convenience wrapper: build a client, build the portfolio, close the client."""
    client = RewardsClient()
    try:
        return build_portfolio(client, **kwargs)  # type: ignore[arg-type]
    finally:
        client.close()
