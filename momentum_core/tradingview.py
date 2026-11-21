"""TradingView-based first-pass universe discovery."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from .core import DynamicUniverseConfig, Security


EXCHANGES = ("NYSE", "NASDAQ", "AMEX")
MAX_MARKET_CAP_RESULTS = 1000


def _records(frame: object) -> list[dict[str, object]]:
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise ValueError("TradingView returned an invalid screener table")
    rows = to_dict(orient="records")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("TradingView returned an invalid screener table")
    return rows


def parse_tradingview_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    min_price: float = 5.0,
) -> list[Security]:
    """Validate screener records and return normalized, supported listings."""
    securities: dict[str, Security] = {}
    for row in rows:
        try:
            ticker = row["ticker"]
            exchange = row["exchange"]
            instrument_type = row["type"]
            type_specs = row["typespecs"]
            close = float(row["close"])
            market_cap = float(row["market_cap_basic"])
            industry = row["industry"]
            earnings_dates = []
            for field in ("earnings_release_date", "earnings_release_next_date"):
                timestamp = row[field]
                if timestamp is None:
                    continue
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, (int, float))
                    or not math.isfinite(timestamp)
                ):
                    raise ValueError(f"Invalid {field} for {ticker}")
                earnings_dates.append(
                    datetime.fromtimestamp(timestamp, ZoneInfo("America/New_York")).date()
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("TradingView row is missing required security fields") from exc
        if not isinstance(industry, str) or not industry.strip():
            raise ValueError(f"TradingView industry is missing for {ticker}")
        if not isinstance(ticker, str) or ":" not in ticker:
            raise ValueError("TradingView returned an invalid ticker")
        if not isinstance(exchange, str) or exchange not in EXCHANGES:
            raise ValueError(f"Unsupported TradingView exchange: {exchange!r}")
        if not math.isfinite(close) or close < min_price:
            raise ValueError(f"TradingView price is below the screening threshold for {ticker}")
        if not isinstance(type_specs, (list, tuple)):
            raise ValueError(f"Unsupported TradingView security type for {ticker}")
        ticker_exchange, symbol = ticker.split(":", 1)
        if ticker_exchange != exchange:
            raise ValueError(f"TradingView exchange mismatch for {ticker}")
        if "/" in symbol:
            # Slash suffixes mark warrants/rights/units (e.g. MSGK/I), not tradeable shares.
            continue
        if instrument_type == "dr":
            normalized_type = "adr"
            is_adr = True
            is_reit = False
        elif instrument_type == "stock" and "common" in type_specs:
            is_reit = "reit" in type_specs
            normalized_type = "reit" if is_reit else "common_stock"
            is_adr = False
        else:
            raise ValueError(f"TradingView security is not an ordinary share: {ticker}")
        if not math.isfinite(market_cap) or market_cap <= 0:
            raise ValueError(f"Invalid market capitalization for {ticker}")
        security = Security(
            symbol=symbol,
            exchange=exchange,
            asset_type=normalized_type,
            market_cap=market_cap,
            is_adr=is_adr,
            is_reit=is_reit,
            industry=industry,
            earnings_dates=tuple(earnings_dates),
        )
        if security.symbol in securities:
            raise ValueError(f"Duplicate TradingView listing: {security.symbol}")
        securities[security.symbol] = security
    return list(securities.values())


def fetch_tradingview_securities(
    config: DynamicUniverseConfig = DynamicUniverseConfig(),
) -> list[Security]:
    """Fetch US-listed common stocks, depositary receipts and REITs, ranked by cap."""
    if config.market_cap_top > MAX_MARKET_CAP_RESULTS:
        raise ValueError(
            f"market_cap_top cannot exceed TradingView's "
            f"{MAX_MARKET_CAP_RESULTS}-row result limit"
        )
    try:
        from requests import RequestException
        from tradingview_screener import And, Or, col, stocks
    except ImportError as exc:
        raise RuntimeError(
            "Install momentum dependencies with "
            "`python3 -m pip install -r requirements.txt`"
        ) from exc

    conditions = [
        col("exchange").isin(EXCHANGES),
        col("close") >= config.min_price,
        col("market_cap_basic") > 0,
    ]
    if config.exclude_reits:
        conditions.append(col("typespecs").has_none_of(["reit"]))

    query = stocks("america").select(
        "exchange", "type", "typespecs", "close", "market_cap_basic",
        "industry", "earnings_release_date", "earnings_release_next_date",
    )
    if config.exclude_adrs:
        query.where(
            *conditions,
            col("type") == "stock",
            col("typespecs").has(["common"]),
        )
    else:
        query.where2(And(
            *conditions,
            Or(
                And(col("type") == "stock", col("typespecs").has(["common"])),
                col("type") == "dr",
            ),
        ))
    query.order_by("market_cap_basic", ascending=False).limit(config.market_cap_top)
    try:
        count, frame = query.get_scanner_data()
    except RequestException as exc:
        raise RuntimeError(f"TradingView screener request failed: {exc}") from exc
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("TradingView returned an invalid result count")
    rows = _records(frame)
    if count and not rows:
        raise ValueError("TradingView returned no rows for a non-empty result")
    securities = parse_tradingview_rows(
        rows, min_price=config.min_price,
    )
    if config.exclude_reits:
        securities = [security for security in securities if not security.is_reit]
    if config.exclude_adrs:
        securities = [security for security in securities if not security.is_adr]
    ranked = sorted(
        securities, key=lambda security: (-security.market_cap, security.symbol),
    )
    return ranked[:config.market_cap_top]
