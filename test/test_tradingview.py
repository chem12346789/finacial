"""Offline tests for the TradingView screener adapter."""

from types import SimpleNamespace
from datetime import date, datetime, timezone
import sys
import unittest
from unittest.mock import patch

from momentum_core import DynamicUniverseConfig, fetch_tradingview_securities
from momentum_core.tradingview import parse_tradingview_rows


def row(ticker, exchange, asset_type, close, market_cap):
    return {
        "ticker": f"{exchange}:{ticker}",
        "exchange": exchange,
        "type": "stock",
        "typespecs": ["common", "reit"] if asset_type == "REIT" else ["common"],
        "close": close,
        "market_cap_basic": market_cap,
        "industry": "Semiconductors",
        "earnings_release_date": None,
        "earnings_release_next_date": None,
    }


class _Column:
    def __init__(self, name):
        self.name = name

    def __ge__(self, value):
        return (self.name, ">=", value)

    def __gt__(self, value):
        return (self.name, ">", value)

    def isin(self, values):
        return (self.name, "in", tuple(values))

    def has(self, values):
        return (self.name, "has", tuple(values))

    def has_none_of(self, values):
        return (self.name, "has_none_of", tuple(values))


class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, *, orient):
        if orient != "records":
            raise AssertionError(orient)
        return self.rows


class _Query:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls
        self.start = 0
        self.count = 0

    def select(self, *fields):
        self.calls.append(("select", fields))
        return self

    def where(self, *conditions):
        self.calls.append(("where", conditions))
        return self

    def where2(self, condition):
        self.calls.append(("where2", condition))
        return self

    def order_by(self, field, *, ascending):
        self.calls.append(("order_by", field, ascending))
        return self

    def offset(self, value):
        self.start = value
        return self

    def limit(self, value):
        self.count = value
        return self

    def get_scanner_data(self):
        page = self.rows[self.start:self.start + self.count]
        return len(self.rows), _Frame(page)


class TradingViewTest(unittest.TestCase):
    def test_reads_industry_and_both_earnings_dates_in_new_york_time(self):
        record = row("A", "NYSE", "Common Stock", 10, 100)
        record.update(
            industry="Biotechnology",
            earnings_release_date=datetime(2026, 10, 15, 23, tzinfo=timezone.utc).timestamp(),
            earnings_release_next_date=datetime(2026, 10, 17, 0, tzinfo=timezone.utc).timestamp(),
        )
        security, = parse_tradingview_rows([record])
        self.assertEqual(security.industry, "Biotechnology")
        self.assertEqual(security.earnings_dates, (date(2026, 10, 15), date(2026, 10, 16)))

    def test_missing_or_invalid_jump_risk_metadata_is_rejected(self):
        for field in ("industry", "earnings_release_date", "earnings_release_next_date"):
            record = row("A", "NYSE", "Common Stock", 10, 100)
            del record[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_tradingview_rows([record])
        for value in ("not-a-date", float("nan"), True):
            record = row("A", "NYSE", "Common Stock", 10, 100)
            record["earnings_release_next_date"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_tradingview_rows([record])

    def test_fetch_filters_and_ranks_screener_rows(self):
        rows = [
            row("BIG", "NASDAQ", "Common Stock", 15, 300),
            row("TRUST", "NYSE", "REIT", 20, 200),
            row("SMALL", "NYSE", "Common Stock", 8, 100),
        ]
        calls = []

        def stocks(market):
            self.assertEqual(market, "america")
            return _Query(rows, calls)

        fake_module = SimpleNamespace(
            col=_Column,
            stocks=stocks,
            And=lambda *conditions: ("and", conditions),
            Or=lambda *conditions: ("or", conditions),
        )
        with patch.dict(sys.modules, {"tradingview_screener": fake_module}):
            securities = fetch_tradingview_securities(DynamicUniverseConfig(
                min_price=5, market_cap_top=2, exclude_reits=False, exclude_adrs=True,
            ))

        self.assertEqual(
            [(security.symbol, security.market_cap) for security in securities],
            [("BIG", 300), ("TRUST", 200)],
        )
        self.assertEqual(
            [call[0] for call in calls],
            ["select", "where", "order_by"],
        )
        self.assertIn(
            ("exchange", "in", ("NYSE", "NASDAQ", "AMEX")),
            calls[1][1],
        )
        self.assertIn(("close", ">=", 5), calls[1][1])
        self.assertIn(("typespecs", "has", ("common",)), calls[1][1])

    def test_includes_reits_by_default_can_exclude_and_rejects_invalid_records(self):
        rows = [
            row("A", "NYSE", "Common Stock", 10, 200),
            row("B", "NASDAQ", "REIT", 10, 300),
        ]
        fake_module = SimpleNamespace(
            col=_Column,
            stocks=lambda market: _Query(rows, []),
            And=lambda *conditions: ("and", conditions),
            Or=lambda *conditions: ("or", conditions),
        )
        with patch.dict(sys.modules, {"tradingview_screener": fake_module}):
            securities = fetch_tradingview_securities()
        self.assertEqual([security.symbol for security in securities], ["B", "A"])
        self.assertTrue(securities[0].is_reit)
        with patch.dict(sys.modules, {"tradingview_screener": fake_module}):
            securities = fetch_tradingview_securities(DynamicUniverseConfig(
                exclude_reits=True,
            ))
        self.assertEqual([security.symbol for security in securities], ["A"])

        invalid_records = (
            row("A", "OTC", "Common Stock", 10, 100),
            {
                **row("A", "NYSE", "Common Stock", 10, 100),
                "typespecs": ["preferred"],
            },
            row("A", "NYSE", "Common Stock", 4, 100),
        )
        for record in invalid_records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                parse_tradingview_rows([record])

    def test_slash_suffixed_warrants_and_units_are_skipped(self):
        securities = parse_tradingview_rows([
            row("MSGK/I", "NASDAQ", "Common Stock", 10, 100),
            row("BRK-B", "NYSE", "Common Stock", 10, 200),
        ])
        self.assertEqual([security.symbol for security in securities], ["BRK.B"])

    def test_includes_depositary_receipts_by_default_and_can_exclude_them(self):
        adr = {
            **row("ADR", "NYSE", "Common Stock", 10, 100),
            "type": "dr",
            "typespecs": [""],
        }
        calls = []
        fake_module = SimpleNamespace(
            col=_Column,
            stocks=lambda market: _Query([adr], calls),
            And=lambda *conditions: ("and", conditions),
            Or=lambda *conditions: ("or", conditions),
        )
        with patch.dict(sys.modules, {"tradingview_screener": fake_module}):
            securities = fetch_tradingview_securities()
        self.assertEqual([(item.symbol, item.asset_type, item.is_adr) for item in securities], [
            ("ADR", "adr", True),
        ])
        self.assertEqual(calls[1][0], "where2")
        calls.clear()
        with patch.dict(sys.modules, {"tradingview_screener": fake_module}):
            securities = fetch_tradingview_securities(DynamicUniverseConfig(
                exclude_adrs=True,
            ))
        self.assertEqual(securities, [])
        self.assertEqual(calls[1][0], "where")


if __name__ == "__main__":
    unittest.main()
