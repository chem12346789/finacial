"""Core implementation for read-only momentum screening."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
import math
from pathlib import Path
import re
from statistics import correlation, linear_regression, stdev
from typing import Mapping, Sequence

from trading_schedule import is_session, previous_session


def _symbol(value: str) -> str:
    symbol = value.strip().upper()
    if symbol.startswith("US."):
        symbol = symbol[3:]
    if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*", symbol):
        raise ValueError(f"Invalid US stock symbol: {value!r}")
    # Treat Yahoo's BRK-B and the exchange's BRK.B as the same share class.
    return symbol.replace("-", ".")


def _rows(path: str | Path, fields: set[str]) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not fields.issubset(reader.fieldnames):
            raise ValueError(f"{path}: required CSV columns: {', '.join(sorted(fields))}")
        rows = []
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path}:{line}: malformed CSV row")
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: CSV has no data rows")
    return rows


@dataclass(frozen=True)
class Universe:
    as_of: date
    members: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Security:
    symbol: str
    exchange: str
    asset_type: str
    market_cap: float
    is_adr: bool = False
    is_reit: bool = False
    industry: str = ""
    earnings_dates: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if self.exchange not in {"NYSE", "NASDAQ", "AMEX"}:
            raise ValueError("exchange must be NYSE, NASDAQ, or AMEX")
        if self.asset_type not in {"common_stock", "reit", "adr"}:
            raise ValueError("asset_type must be common_stock, reit, or adr")
        if (
            isinstance(self.market_cap, bool)
            or not isinstance(self.market_cap, (int, float))
            or not math.isfinite(self.market_cap)
            or self.market_cap <= 0
        ):
            raise ValueError("market_cap must be finite and positive")
        if type(self.is_adr) is not bool or type(self.is_reit) is not bool:
            raise ValueError("is_adr and is_reit must be booleans")


@dataclass(frozen=True)
class DailyBar:
    close: float
    volume: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.close, bool)
            or not isinstance(self.close, (int, float))
            or not math.isfinite(self.close)
            or self.close <= 0
        ):
            raise ValueError("close must be finite and positive")
        if (
            isinstance(self.volume, bool)
            or not isinstance(self.volume, (int, float))
            or not math.isfinite(self.volume)
            or self.volume < 0
        ):
            raise ValueError("volume must be finite and non-negative")


@dataclass(frozen=True)
class DynamicUniverseConfig:
    min_price: float = 5.0
    adv_days: int = 20
    adv_top: int = 1000
    market_cap_top: int = 1000
    exclude_adrs: bool = False
    exclude_reits: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.min_price, bool)
            or not isinstance(self.min_price, (int, float))
            or not math.isfinite(self.min_price)
            or self.min_price <= 0
        ):
            raise ValueError("min_price must be finite and positive")
        for name in ("adv_days", "adv_top", "market_cap_top"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.exclude_adrs) is not bool or type(self.exclude_reits) is not bool:
            raise ValueError("exclude_adrs and exclude_reits must be booleans")


@dataclass(frozen=True)
class DynamicUniverseResult:
    universe: Universe
    eligible_count: int
    excluded: tuple[dict[str, str], ...]


def build_dynamic_universe(
    securities: Sequence[Security],
    bars: Mapping[str, Mapping[date, DailyBar]],
    *,
    as_of: date,
    config: DynamicUniverseConfig = DynamicUniverseConfig(),
) -> DynamicUniverseResult:
    """Build a rebalance-date universe from listings and daily close/volume bars."""
    security_by_symbol: dict[str, Security] = {}
    for security in securities:
        symbol = _symbol(security.symbol)
        if symbol in security_by_symbol:
            raise ValueError(f"Duplicate security listing: {symbol}")
        security_by_symbol[symbol] = security

    calendar = sorted({
        day
        for symbol in security_by_symbol
        for day in bars.get(symbol, {})
        if day <= as_of
    })
    if as_of not in calendar:
        raise ValueError("as_of must be an available completed trading session")
    if len(calendar) < config.adv_days:
        raise ValueError(f"Need at least {config.adv_days} sessions for ADV")
    window = calendar[-config.adv_days:]
    # Invert the event-relative [-2, +1] window to find releases around as_of.
    blackout_start = previous_session(as_of)
    blackout_end = as_of
    for _ in range(2):
        blackout_end += timedelta(days=1)
        while not is_session(blackout_end):
            blackout_end += timedelta(days=1)

    price_eligible: list[Security] = []
    base: list[tuple[Security, float]] = []
    excluded: list[dict[str, str]] = []
    for symbol, security in sorted(security_by_symbol.items()):
        reason = ""
        industry = security.industry.strip().casefold()
        earnings_sessions = []
        for event_date in security.earnings_dates:
            while not is_session(event_date):
                event_date += timedelta(days=1)
            earnings_sessions.append(event_date)
        if (
            industry == "biotechnology"
            or industry.startswith("pharmaceuticals")
            or industry.startswith(("352010", "352020"))
        ):
            reason = "excluded_industry"
        elif any(blackout_start <= day <= blackout_end for day in earnings_sessions):
            reason = "earnings_blackout"
        elif config.exclude_adrs and (
            security.is_adr or security.asset_type == "adr"
        ):
            reason = "adr"
        elif config.exclude_reits and (
            security.is_reit or security.asset_type == "reit"
        ):
            reason = "reit"
        else:
            series = bars.get(symbol, {})
            if as_of not in series:
                reason = "missing_as_of_bar"
            elif series[as_of].close < config.min_price:
                reason = "price_below_minimum"
            else:
                price_eligible.append(security)
                if any(day not in series for day in window):
                    reason = "insufficient_adv_history"
                else:
                    dollar_volumes = [
                        series[day].close * series[day].volume for day in window
                    ]
                    if not all(math.isfinite(value) for value in dollar_volumes):
                        raise ValueError(f"{symbol}: daily dollar volume is not finite")
                    adv = math.fsum(dollar_volumes) / config.adv_days
                    if not math.isfinite(adv):
                        raise ValueError(f"{symbol}: average dollar volume is not finite")
                    if adv <= 0:
                        reason = "no_dollar_volume"
                    else:
                        base.append((security, adv))
        if reason:
            excluded.append({"symbol": symbol, "reason": reason})

    adv_ranked = sorted(base, key=lambda item: (-item[1], item[0].symbol))
    adv_symbols = {security.symbol for security, _ in adv_ranked[:config.adv_top]}
    cap_ranked = sorted(
        price_eligible,
        key=lambda security: (-security.market_cap, security.symbol),
    )
    cap_symbols = {security.symbol for security in cap_ranked[:config.market_cap_top]}
    members: dict[str, tuple[str, ...]] = {}
    for security, _ in base:
        symbol = security.symbol
        reasons = []
        if symbol not in adv_symbols:
            reasons.append("adv_below_rank_cutoff")
        if symbol not in cap_symbols:
            reasons.append("market_cap_below_rank_cutoff")
        if reasons:
            excluded.append({"symbol": symbol, "reason": ",".join(reasons)})
        else:
            members[symbol] = (security.exchange,)

    excluded.sort(key=lambda row: row["symbol"])
    universe = Universe(as_of, members)
    return DynamicUniverseResult(universe, len(members), tuple(excluded))


def load_universe(path: str | Path) -> Universe:
    """Load matching Russell 1000, VOO, and QQQ snapshots."""
    members: dict[str, set[str]] = {}
    dates: set[date] = set()
    sources: set[str] = set()
    for row in _rows(path, {"as_of", "source", "symbol"}):
        dates.add(date.fromisoformat(row["as_of"].strip()))
        source = row["source"].strip().upper()
        if source not in {"RUSSELL1000", "VOO", "QQQ"}:
            raise ValueError(
                f"Unsupported source: {source!r}; expected RUSSELL1000, VOO, or QQQ"
            )
        symbol = _symbol(row["symbol"])
        if symbol in {"VOO", "QQQ", "RUSSELL1000"}:
            raise ValueError("Supply constituent stocks, not an ETF or index itself")
        sources.add(source)
        members.setdefault(symbol, set()).add(source)
    required_sources = {"RUSSELL1000", "VOO", "QQQ"}
    if sources != required_sources or len(dates) != 1:
        raise ValueError(
            "Supply Russell 1000, VOO, and QQQ constituents with one common as_of date"
        )
    return Universe(
        dates.pop(),
        {symbol: tuple(sorted(sources)) for symbol, sources in sorted(members.items())},
    )


def load_prices(path: str | Path) -> dict[str, dict[date, float]]:
    """Read adjusted closes; reject invalid prices and duplicate observations."""
    prices: dict[str, dict[date, float]] = {}
    for row in _rows(path, {"date", "symbol", "adjusted_close"}):
        day = date.fromisoformat(row["date"].strip())
        symbol = _symbol(row["symbol"])
        close = float(row["adjusted_close"])
        if not math.isfinite(close) or close <= 0:
            raise ValueError(f"{symbol} {day}: adjusted_close must be finite and positive")
        series = prices.setdefault(symbol, {})
        if day in series:
            raise ValueError(f"{symbol} {day}: duplicate price observation")
        series[day] = close
    return prices


MOMENTUM_METHODS = ("volatility_scaled", "roc", "sharpe", "regression", "ensemble")

ENSEMBLE_WEIGHTS = {
    "volatility_scaled": 1 / 6,
    "sharpe": 1 / 6,
    "roc": 1 / 3,
    "regression": 1 / 3,
}


@dataclass(frozen=True)
class MomentumConfig:
    lookback: int = 21
    skip: int = 0
    top: int = 20
    positive_only: bool = False
    method: str = "volatility_scaled"

    def __post_init__(self) -> None:
        for name in ("lookback", "skip", "top"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if not 0 <= self.skip < self.lookback or self.top <= 0:
            raise ValueError("Require 0 <= skip < lookback and top > 0")
        if self.lookback - self.skip < 2:
            raise ValueError("Risk-adjusted momentum requires at least 2 daily returns")
        if type(self.positive_only) is not bool:
            raise ValueError("positive_only must be a boolean")
        if self.method not in MOMENTUM_METHODS:
            raise ValueError(f"Unknown momentum method: {self.method}")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    sources: tuple[str, ...]
    score: float
    start_date: str
    end_date: str
    start_price: float
    end_price: float
    raw_return: float
    daily_volatility: float


def _momentum_score(
    method: str, closes: list[float], daily_returns: list[float],
    raw_return: float, daily_volatility: float,
) -> float:
    if method == "roc":
        return raw_return
    if method == "volatility_scaled":
        return raw_return / daily_volatility
    if method == "sharpe":
        return math.fsum(daily_returns) / len(daily_returns) / daily_volatility
    logs = [math.log(close) for close in closes]
    if max(logs) == min(logs):
        return 0.0
    days = range(len(logs))
    slope = linear_regression(days, logs).slope
    r_squared = correlation(days, logs) ** 2
    return slope * r_squared


def _screen_ensemble(
    universe: Universe, prices: dict[str, dict[date, float]], *,
    as_of: date, config: MomentumConfig,
) -> dict:
    results = {
        method: screen(
            universe, prices, as_of=as_of,
            config=replace(config, method=method, top=max(1, len(universe.members))),
        )
        for method in ENSEMBLE_WEIGHTS
    }
    rows = {
        method: {row["symbol"]: row for row in result["candidates"]}
        for method, result in results.items()
    }
    eligible = set.intersection(*(set(candidates) for candidates in rows.values()))
    method_ranks: dict[str, dict[str, float]] = {}
    for method, candidates in rows.items():
        ordered = sorted(eligible, key=lambda symbol: (-candidates[symbol]["score"], symbol))
        ranks: dict[str, float] = {}
        position = 0
        while position < len(ordered):
            end = position + 1
            while end < len(ordered) and (
                candidates[ordered[end]]["score"] == candidates[ordered[position]]["score"]
            ):
                end += 1
            for symbol in ordered[position:end]:
                ranks[symbol] = (position + 1 + end) / 2
            position = end
        method_ranks[method] = ranks
    candidates = []
    for symbol in sorted(eligible):
        ranks = {method: method_ranks[method][symbol] for method in ENSEMBLE_WEIGHTS}
        risk_rank = (ranks["volatility_scaled"] + ranks["sharpe"]) / 2
        average_rank = (risk_rank + ranks["roc"] + ranks["regression"]) / 3
        candidates.append({
            **rows["roc"][symbol],
            "score": -average_rank,
            "average_rank": average_rank,
            "risk_adjusted_rank": risk_rank,
            "method_ranks": ranks,
            "method_scores": {
                method: rows[method][symbol]["score"] for method in ENSEMBLE_WEIGHTS
            },
        })
    candidates.sort(key=lambda row: (row["average_rank"], row["symbol"]))
    excluded = {
        row["symbol"]: row
        for result in reversed(list(results.values())) for row in result["excluded"]
    }
    return {
        **results["roc"],
        "config": asdict(config),
        "scoring": {
            "method": "ensemble", "weights": ENSEMBLE_WEIGHTS.copy(),
            "tie_method": "average", "rank_direction": "ascending",
            "score": "negative_average_rank",
        },
        "eligible_count": len(eligible),
        "excluded": [excluded[symbol] for symbol in sorted(excluded)],
        "candidates": [
            {**row, "rank": rank}
            for rank, row in enumerate(candidates[:config.top], 1)
        ],
    }


def screen(
    universe: Universe,
    prices: dict[str, dict[date, float]],
    *,
    as_of: date,
    config: MomentumConfig = MomentumConfig(),
) -> dict:
    """Rank on a shared session grid; never fill gaps or use future prices."""
    if config.method == "ensemble":
        return _screen_ensemble(universe, prices, as_of=as_of, config=config)
    if universe.as_of > as_of:
        raise ValueError("Holdings snapshot is newer than the screening date")
    calendar = sorted({
        day
        for symbol in universe.members
        for day in prices.get(symbol, {})
        if day <= as_of
    })
    if not calendar:
        raise ValueError("No constituent prices on or before as_of")
    if calendar[-1] != as_of:
        raise ValueError("as_of must be an available completed trading session")
    if len(calendar) <= config.lookback:
        raise ValueError(f"Need at least {config.lookback + 1} shared sessions")
    window = calendar[-config.lookback - 1:]
    start, end = window[0], window[-config.skip - 1]
    ranked: list[Candidate] = []
    excluded: list[dict[str, str]] = []
    for symbol, sources in sorted(universe.members.items()):
        series = prices.get(symbol, {})
        reason = ""
        if not any(day <= as_of for day in series):
            reason = "missing_prices"
        elif as_of not in series:
            reason = "stale_prices"
        elif start not in series:
            reason = "insufficient_history"
        elif any(day not in series for day in window):
            reason = "missing_sessions"
        else:
            for day in window:
                if not math.isfinite(series[day]) or series[day] <= 0:
                    raise ValueError(f"{symbol} {day}: invalid adjusted close")
            raw_return = series[end] / series[start] - 1
            signal_days = window[:len(window) - config.skip]
            daily_returns = [
                series[current] / series[previous] - 1
                for previous, current in zip(signal_days, signal_days[1:])
            ]
            if not math.isfinite(raw_return) or not all(
                math.isfinite(value) for value in daily_returns
            ):
                raise ValueError(f"{symbol}: momentum score is not finite")
            daily_volatility = stdev(daily_returns)
            if daily_volatility == 0 and config.method in ("volatility_scaled", "sharpe"):
                reason = "zero_volatility"
            elif config.positive_only and raw_return <= 0:
                reason = "non_positive_momentum"
            else:
                score = _momentum_score(
                    config.method, [series[day] for day in signal_days],
                    daily_returns, raw_return, daily_volatility,
                )
                if not math.isfinite(score):
                    raise ValueError(f"{symbol}: momentum score is not finite")
                ranked.append(Candidate(
                    symbol, sources, score, start.isoformat(), end.isoformat(),
                    series[start], series[end], raw_return, daily_volatility,
                ))
        if reason:
            excluded.append({"symbol": symbol, "reason": reason})
    ranked.sort(key=lambda candidate: (-candidate.score, candidate.symbol))
    return {
        "as_of": as_of.isoformat(),
        "holdings_as_of": universe.as_of.isoformat(),
        "config": asdict(config),
        "scoring": {
            "method": config.method,
            "return_type": "simple",
            "volatility": "sample_daily_std",
            "ddof": 1,
            "annualized": False,
        },
        "universe_size": len(universe.members),
        "eligible_count": len(ranked),
        "excluded": excluded,
        "candidates": [
            {"rank": rank, **asdict(candidate)}
            for rank, candidate in enumerate(ranked[:config.top], 1)
        ],
    }
