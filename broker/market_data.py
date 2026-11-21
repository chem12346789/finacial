"""Shared Futu quote-history clients and daily K-line helpers."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from typing import Iterator, Protocol, Sequence
from zoneinfo import ZoneInfo

from ouput.paths import DEFAULT_CACHE_PATH

from .futu_client import FutuClient
from .client import FutuAPIError
from .credentials import get_store


class HistoryClient(Protocol):
    def get_history_kline(
        self,
        code: str,
        end: str,
        *,
        start: str | None = None,
        ktype: int,
        autype: int,
        num: int,
        extended_time: int,
    ) -> object: ...


def get_default_quote_client(profile: str = "default") -> FutuClient:
    """Create a quote client from the selected credential-store profile."""
    return get_store(profile).client()


def futu_bar_date(bar: dict, symbol: str) -> date:
    try:
        if "date" in bar:
            return datetime.strptime(str(bar["date"]), "%Y%m%d").date()
        value = bar["time_key"]
        if isinstance(value, str):
            return date.fromisoformat(value[:10])
        if type(value) is not int:
            raise ValueError("time_key must be an integer millisecond timestamp")
        if "time_zone" in bar:
            offset = bar["time_zone"]
            if type(offset) is not int:
                raise ValueError("time_zone must be an integer minute offset")
            local_zone = timezone(timedelta(minutes=offset))
        else:
            local_zone = ZoneInfo("America/New_York")
        return datetime.fromtimestamp(value / 1000, tz=local_zone).date()
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError(
            f"Invalid Futu K-line row for {symbol}: invalid date/time_key/time_zone"
        ) from exc


def futu_kline_rows(response: object, symbol: str) -> Iterator[dict]:
    if not isinstance(response, dict) or not isinstance(
        response.get("kline_list"), list
    ):
        raise ValueError(f"Invalid Futu K-line response for {symbol}")
    for bar in response["kline_list"]:
        if not isinstance(bar, dict):
            raise ValueError(f"Invalid Futu K-line row for {symbol}")
        yield bar


def fetch_daily_klines(
    client: HistoryClient,
    symbols: Sequence[str],
    *,
    end: date,
    autype: int,
    num: int,
    start: date | None = None,
) -> dict[str, dict[date, dict]]:
    """Fetch and validate one page of daily bars for each US symbol."""
    if type(num) is not int or not 1 <= num <= 370:
        raise ValueError("num must be between 1 and Futu's 370-bar limit")
    if start is not None and start > end:
        raise ValueError("start must not be after end")

    result: dict[str, dict[date, dict]] = {}
    for symbol in symbols:
        code = symbol if symbol.startswith("US.") else f"US.{symbol}"
        response = client.get_history_kline(
            code,
            end.isoformat(),
            **(
                {"start": start.isoformat()}
                if start is not None else {}
            ),
            ktype=2,
            autype=autype,
            num=num,
            extended_time=0,
        )
        rows: dict[date, dict] = {}
        for bar in futu_kline_rows(response, code):
            day = futu_bar_date(bar, code)
            try:
                close = float(bar["close"])
                if autype == 0:
                    volume = float(bar["volume"])
                    if not math.isfinite(volume) or volume < 0:
                        raise ValueError("invalid volume")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid Futu K-line row for {code}: close/volume"
                ) from exc
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"{code} {day}: close must be finite and positive")
            if day in rows:
                raise ValueError(f"{code} {day}: duplicate Futu K-line")
            if day <= end and (start is None or day >= start):
                rows[day] = bar
        result[symbol] = rows
    return result


class ThrottledHistoryClient:
    def __init__(self, client: HistoryClient, interval: float = 2.0) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("request interval must be finite and positive")
        self.client = client
        self.initial_interval = interval
        self.retry_count = 0
        self.next_request_at = 0.0

    @property
    def interval(self) -> float:
        return self.initial_interval + self.retry_count * 0.5

    def get_history_kline(
        self, code: str, end: str, *, ktype: int, autype: int,
        num: int, extended_time: int, start: str | None = None,
    ) -> object:
        for attempt in range(5):
            delay = self.next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                response = self.client.get_history_kline(
                    code, end, ktype=ktype, autype=autype,
                    num=num, extended_time=extended_time,
                    **({"start": start} if start is not None else {}),
                )
            except FutuAPIError as exc:
                if not (
                    exc.status == 429
                    or exc.code == 429
                    or "rate limit exceeded" in str(exc).lower()
                ):
                    raise
                self.retry_count += 1
                if attempt == 4:
                    raise
                wait = self.retry_count * 2
                print(
                    f"{code}: Futu rate limit; retry {attempt + 1}/4 "
                    f"after {wait}s; retry_count={self.retry_count}; "
                    f"subsequent request interval={self.interval:g}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                self.retry_count = max(0, self.retry_count - 1)
                return response
            finally:
                self.next_request_at = time.monotonic() + self.interval


class CachedHistoryClient:
    def __init__(
        self, client: HistoryClient, path: Path, *, refresh: bool = False,
    ) -> None:
        self.client = client
        self.path = path
        self.refresh = refresh
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS daily_kline_cache (
                    code TEXT, ktype INTEGER, autype INTEGER, extended_time INTEGER,
                    end TEXT, num INTEGER, response TEXT NOT NULL,
                    PRIMARY KEY (code, ktype, autype, extended_time, end, num)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS daily_kline_range_cache (
                    code TEXT, ktype INTEGER, autype INTEGER, extended_time INTEGER,
                    start TEXT, end TEXT, num INTEGER, fetched_on TEXT,
                    response TEXT NOT NULL,
                    PRIMARY KEY (code, ktype, autype, extended_time, start, end, num)
                )"""
            )

    @staticmethod
    def _rows(response: object, code: str, end: date, autype: int) -> dict[date, dict]:
        rows: dict[date, dict] = {}
        for bar in futu_kline_rows(response, code):
            day = futu_bar_date(bar, code)
            try:
                close = float(bar["close"])
                if autype == 0:
                    volume = float(bar["volume"])
                    if not math.isfinite(volume) or volume < 0:
                        raise ValueError("invalid volume")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid Futu K-line row for {code}: close/volume"
                ) from exc
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"{code} {day}: close must be finite and positive")
            if day in rows:
                raise ValueError(f"{code} {day}: duplicate Futu K-line")
            if day <= end:
                rows[day] = bar
        return rows

    def get_history_kline(
        self, code: str, end: str, *, ktype: int, autype: int,
        num: int, extended_time: int, start: str | None = None,
    ) -> object:
        kwargs = dict(ktype=ktype, autype=autype, num=num, extended_time=extended_time)
        end_date = date.fromisoformat(end)
        today = datetime.now(ZoneInfo("America/New_York")).date()
        if ktype == 2 and (start is not None or end_date >= today):
            key = (code, ktype, autype, extended_time, start or "", end, num)
            if not self.refresh and end_date < today:
                with closing(sqlite3.connect(self.path)) as connection:
                    cached_range = connection.execute(
                        """SELECT response FROM daily_kline_range_cache
                           WHERE code=? AND ktype=? AND autype=? AND extended_time=?
                             AND start=? AND end=? AND num=? AND fetched_on>?""",
                        (*key, end),
                    ).fetchone()
                if cached_range is not None:
                    return json.loads(cached_range[0])
            response = self.client.get_history_kline(
                code, end, **kwargs, **({"start": start} if start is not None else {}),
            )
            self._rows(response, code, end_date, autype)
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute(
                    """INSERT OR REPLACE INTO daily_kline_range_cache
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*key, today.isoformat(), json.dumps(response, allow_nan=False)),
                )
            return response
        if ktype != 2:
            return self.client.get_history_kline(
                code, end, **kwargs, **({"start": start} if start is not None else {}),
            )
        key = (code, ktype, autype, extended_time)
        cached = None
        if not self.refresh:
            with closing(sqlite3.connect(self.path)) as connection:
                cached = connection.execute(
                    """SELECT end, response FROM daily_kline_cache
                       WHERE code=? AND ktype=? AND autype=? AND extended_time=?
                         AND end<=? AND num>=?
                       ORDER BY end DESC, num ASC LIMIT 1""",
                    (*key, end, num),
                ).fetchone()
        old_rows: dict[date, dict] = {}
        if cached is not None:
            response = json.loads(cached[1])
            old_rows = self._rows(response, code, end_date, autype)
            if cached[0] == end:
                return {**response, "kline_list": [
                    old_rows[day] for day in sorted(old_rows)[-num:]
                ]}
        response = None
        if old_rows:
            anchor = max(old_rows)
            count = (end_date - anchor).days + 1
            if count < num:
                response = self.client.get_history_kline(
                    code, end, **{**kwargs, "num": count}, start=anchor.isoformat(),
                )
                new_rows = self._rows(response, code, end_date, autype)
                overlap = old_rows.keys() & new_rows.keys()
                if anchor not in overlap or (
                    autype == 1 and any(
                        float(old_rows[day]["close"]) != float(new_rows[day]["close"])
                        for day in overlap
                    )
                ):
                    response = None
                else:
                    new_rows = {**old_rows, **new_rows}
        if response is None:
            response = self.client.get_history_kline(code, end, **kwargs)
            new_rows = self._rows(response, code, end_date, autype)
        result = {**response, "kline_list": [
            new_rows[day] for day in sorted(new_rows)[-num:]
        ]}
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO daily_kline_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*key, end, num, json.dumps(result, allow_nan=False)),
            )
        return result
