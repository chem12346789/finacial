"""Core Asset Risk Parity strategy: read-only momentum selection entry point.

Portfolio weighting and order execution are not connected to this entry point.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from typing import TYPE_CHECKING, Iterator, Sequence, TypeVar

from ouput.momentum import DEFAULT_MOMENTUM_PATH, format_momentum, save_momentum
from ouput.paths import DEFAULT_CACHE_PATH, output_path

from momentum_core import (
    Candidate,
    DailyBar,
    DynamicUniverseConfig,
    DynamicUniverseResult,
    MOMENTUM_METHODS,
    MomentumConfig,
    Security,
    Universe,
    build_dynamic_universe,
    fetch_tradingview_securities,
    load_prices,
    load_universe,
    screen,
)

if TYPE_CHECKING:
    from broker.market_data import HistoryClient

_T = TypeVar("_T")


class _DownloadProgress:
    def __init__(self, total: int, label: str = "Kline") -> None:
        self.total = total
        self.label = label
        self.started = time.monotonic()
        self.completed = 0
        self.interactive = sys.stderr.isatty()
        self.width = 0

    def render(self, stage: str, symbol: str) -> None:
        elapsed = time.monotonic() - self.started
        remaining = (
            elapsed / self.completed * (self.total - self.completed)
            if self.completed else None
        )
        filled = 24 * self.completed // self.total if self.total else 24
        eta = f"{remaining:.0f}s" if remaining is not None else "--"
        detail = f"{stage} {symbol}".strip()
        line = (
            f"{self.label} [{'#' * filled}{'-' * (24 - filled)}] "
            f"{self.completed}/{self.total} {detail} elapsed={elapsed:.0f}s ETA={eta}"
        )
        self.width = max(self.width, len(line))
        print(
            "\r" + line.ljust(self.width) if self.interactive else line,
            end="" if self.interactive else "\n", file=sys.stderr, flush=True,
        )

    def iterate(self, items: Sequence[_T], stage: str) -> Iterator[_T]:
        self.render(stage, "")
        for item in items:
            symbol = item.symbol if isinstance(item, Security) else str(item)
            self.render(stage, symbol)
            yield item
            self.completed += 1
            self.render(stage, symbol)

    def close(self) -> None:
        if self.interactive:
            print(file=sys.stderr, flush=True)


def _progress(
    items: Sequence[_T], label: str, enabled: bool | _DownloadProgress,
) -> Iterator[_T]:
    if isinstance(enabled, _DownloadProgress):
        yield from enabled.iterate(items, label)
    elif enabled:
        with closing(_DownloadProgress(len(items), label)) as progress:
            yield from progress.iterate(items, "")
    else:
        yield from items


__all__ = [
    "Candidate",
    "DailyBar",
    "DynamicUniverseConfig",
    "DynamicUniverseResult",
    "MomentumConfig",
    "Security",
    "Universe",
    "build_dynamic_universe",
    "fetch_tradingview_securities",
    "fetch_futu_daily_bars",
    "fetch_futu_prices",
    "load_prices",
    "load_universe",
    "main",
    "screen",
]


def fetch_futu_prices(
    client: HistoryClient, universe: Universe, *, as_of: date,
    progress: bool | _DownloadProgress = False,
) -> dict[str, dict[date, float]]:
    """Fetch up to 370 forward-adjusted daily bars for each constituent."""
    from broker.market_data import fetch_daily_klines

    prices: dict[str, dict[date, float]] = {}
    for symbol in _progress(list(universe.members), "Momentum", progress):
        series: dict[date, float] = {}
        rows = fetch_daily_klines(
            client, [symbol], end=as_of, autype=1, num=370,
        )[symbol]
        for day, bar in rows.items():
            try:
                close = float(bar["close"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid Futu K-line row for {symbol}: invalid close"
                ) from exc
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"{symbol} {day}: adjusted close must be finite and positive")
            if day in series:
                raise ValueError(f"{symbol} {day}: duplicate Futu K-line")
            if day <= as_of:
                series[day] = close
        prices[symbol] = series
    return prices


def fetch_futu_daily_bars(
    client: HistoryClient,
    securities: Sequence[Security],
    *,
    as_of: date,
    num: int,
    progress: bool | _DownloadProgress = False,
) -> dict[str, dict[date, DailyBar]]:
    """Fetch raw daily close/volume bars for liquidity screening."""
    from broker.market_data import fetch_daily_klines

    if type(num) is not int or not 1 <= num <= 370:
        raise ValueError("num must be between 1 and Futu's 370-bar limit")
    bars: dict[str, dict[date, DailyBar]] = {}
    for security in _progress(securities, "ADV", progress):
        symbol = security.symbol
        series: dict[date, DailyBar] = {}
        rows = fetch_daily_klines(
            client, [symbol], end=as_of, autype=0, num=num,
        )[symbol]
        for day, bar in rows.items():
            try:
                daily_bar = DailyBar(float(bar["close"]), float(bar["volume"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid Futu K-line row for {symbol}: invalid close/volume"
                ) from exc
            if day in series:
                raise ValueError(f"{symbol} {day}: duplicate Futu K-line")
            if day <= as_of:
                series[day] = daily_bar
        bars[symbol] = series
    return bars


def _credentials_client() -> HistoryClient:
    from broker.market_data import get_default_quote_client

    return get_default_quote_client()


def _save_prices(path: Path, prices: dict[str, dict[date, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "symbol", "adjusted_close"])
        for symbol, series in sorted(prices.items()):
            for day, close in sorted(series.items()):
                writer.writerow([day.isoformat(), symbol, close])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tradingview", action="store_true", required=True,
        help="screen the dynamic US stock universe with TradingView",
    )
    parser.add_argument("--save-prices", type=Path)
    parser.add_argument(
        "--format", choices=("table", "json"), default="json",
        help="stdout format (default: json; the saved file is always JSON)",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_MOMENTUM_PATH,
        help="JSON destination (default: project ouput/momentum.json)",
    )
    parser.add_argument(
        "--cache-path", type=Path,
        default=DEFAULT_CACHE_PATH,
        help="persistent Futu daily-bar cache (default: project .cache/finacial/kline.sqlite3)",
    )
    cache_mode = parser.add_mutually_exclusive_group()
    cache_mode.add_argument("--no-cache", action="store_true", help="bypass the K-line cache")
    cache_mode.add_argument(
        "--refresh-cache", action="store_true",
        help="fetch full windows and replace cached responses",
    )
    parser.add_argument(
        "--request-interval", type=float, default=2.0,
        help="initial request interval; adds retry_count * 0.5s, retry wait is retry_count * 2s",
    )
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--max-securities", type=int,
        help="debug/test mode: keep only the first N screener securities before "
             "K-line downloads",
    )
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--adv-days", type=int, default=20)
    parser.add_argument("--adv-top", type=int, default=1000)
    parser.add_argument("--market-cap-top", type=int, default=1000)
    parser.add_argument(
        "--exclude-adrs", action="store_true",
        help="exclude depositary receipts (included by default)",
    )
    reit_mode = parser.add_mutually_exclusive_group()
    reit_mode.add_argument(
        "--exclude-reits", action="store_true",
        help="exclude REITs (included by default)",
    )
    reit_mode.add_argument(
        "--include-reits", action="store_true",
        help="include REITs (already the default)",
    )
    parser.add_argument("--lookback", type=int, default=21)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--positive-only", action="store_true")
    methods = parser.add_mutually_exclusive_group()
    methods.add_argument(
        "--method", choices=MOMENTUM_METHODS, default="volatility_scaled",
        help="momentum scoring method (default: volatility_scaled)",
    )
    methods.add_argument(
        "--methods", nargs="+", choices=MOMENTUM_METHODS,
        help="score several methods from one download; save <output stem>_<method>.json",
    )
    args = parser.parse_args(argv)
    try:
        args.output = output_path(args.output)
        args.cache_path = output_path(args.cache_path)
        if args.save_prices is not None:
            args.save_prices = output_path(args.save_prices)
    except ValueError as exc:
        parser.error(str(exc))
    if not math.isfinite(args.request_interval) or args.request_interval <= 0:
        parser.error("--request-interval must be finite and positive")
    try:
        dynamic_config = DynamicUniverseConfig(
            min_price=args.min_price,
            adv_days=args.adv_days,
            adv_top=args.adv_top,
            market_cap_top=args.market_cap_top,
            exclude_adrs=args.exclude_adrs,
            exclude_reits=args.exclude_reits,
        )
        momentum_configs = [
            MomentumConfig(
                args.lookback, args.skip, args.top, args.positive_only, method,
            )
            for method in dict.fromkeys(args.methods or [args.method])
        ]
    except ValueError as exc:
        parser.error(str(exc))
    expected_errors: tuple[type[Exception], ...] = (
        OSError, ValueError, csv.Error, sqlite3.Error,
    )
    from broker import FutuError

    expected_errors += (FutuError, RuntimeError)
    try:
        from broker.market_data import CachedHistoryClient, ThrottledHistoryClient

        client: HistoryClient = ThrottledHistoryClient(
            _credentials_client(), interval=args.request_interval,
        )
        if not args.no_cache:
            client = CachedHistoryClient(
                client, args.cache_path, refresh=args.refresh_cache,
            )
        securities = fetch_tradingview_securities(dynamic_config)
        screener_snapshot_at = datetime.now(timezone.utc).isoformat()
        if not securities:
            raise ValueError("TradingView returned no price-qualified US listings")
        if args.max_securities is not None:
            if args.max_securities < 1:
                raise ValueError("--max-securities must be at least 1")
            securities = securities[: args.max_securities]
        with closing(_DownloadProgress(2 * len(securities))) as progress:
            bars = fetch_futu_daily_bars(
                client, securities, as_of=args.as_of, num=args.adv_days,
                progress=progress,
            )
            selected = build_dynamic_universe(
                securities, bars, as_of=args.as_of, config=dynamic_config,
            )
            universe = selected.universe
            progress.total = len(securities) + len(universe.members)
            prices = fetch_futu_prices(
                client, universe, as_of=args.as_of, progress=progress,
            )
        if args.save_prices is not None:
            _save_prices(args.save_prices, prices)
        dynamic_universe = {
            "config": {
                "min_price": dynamic_config.min_price,
                "adv_days": dynamic_config.adv_days,
                "adv_top": dynamic_config.adv_top,
                "market_cap_top": dynamic_config.market_cap_top,
                "exclude_adrs": dynamic_config.exclude_adrs,
                "exclude_reits": dynamic_config.exclude_reits,
            },
            "eligible_count": selected.eligible_count,
            "excluded": list(selected.excluded),
            "screener_snapshot_at_utc": screener_snapshot_at,
        }
        results = []
        for config in momentum_configs:
            result = screen(universe, prices, as_of=args.as_of, config=config)
            result["universe_source"] = "TradingView screener + Futu daily bars"
            result["dynamic_universe"] = dynamic_universe
            results.append(result)
        for result in results:
            destination = args.output
            if args.methods is not None:
                destination = destination.with_name(
                    f'{destination.stem}_{result["scoring"]["method"]}{destination.suffix}'
                )
            save_momentum(result, destination)
    except expected_errors as exc:
        parser.error(str(exc))
    if args.methods is not None and args.format == "json":
        print(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for result in results:
            print(format_momentum(result, output_format=args.format))
    return 0 if all(result["candidates"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
