"""US opening-window boundaries, DST and regular holiday exclusions."""

from datetime import date, datetime, time
import unittest
from zoneinfo import ZoneInfo

from trading_schedule import is_session, opening_window, previous_session


class TradingScheduleTest(unittest.TestCase):
    def test_opening_window_exact_boundaries(self):
        day = date(2026, 10, 19)
        for clock, expected in (
            (time(9, 29, 59), False), (time(9, 30), True),
            (time(9, 39, 59), True), (time(9, 40), True), (time(16), True),
            (time(17, 29, 59, 999999), True), (time(17, 30), False),
            (time(17, 30, 0, 1), False),
        ):
            with self.subTest(clock=clock):
                self.assertEqual(opening_window(day, clock), expected)

    def test_dst_keeps_local_open_at_0930(self):
        for utc_hour, month, day in ((13, 10, 19), (14, 11, 2)):
            utc = datetime(2026, month, day, utc_hour, 30, tzinfo=ZoneInfo("UTC"))
            local = utc.astimezone(ZoneInfo("America/New_York"))
            self.assertTrue(opening_window(local.date(), local.time()))
            for minute, expected in ((29, True), (30, False)):
                close = datetime(2026, month, day, utc_hour + 8, minute, tzinfo=ZoneInfo("UTC"))
                local = close.astimezone(ZoneInfo("America/New_York"))
                self.assertEqual(opening_window(local.date(), local.time()), expected)

    def test_holidays_weekends_and_good_friday(self):
        for day in (
            date(2026, 10, 24), date(2026, 1, 19), date(2026, 2, 16),
            date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
            date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
        ):
            with self.subTest(day=day):
                self.assertFalse(opening_window(day, time(9, 30)))
                self.assertFalse(opening_window(day, time(17, 29)))
        self.assertTrue(is_session(date(2026, 10, 12)))  # Columbus Day is not an equity holiday.
        self.assertTrue(is_session(date(2021, 12, 31)))

    def test_previous_session_skips_observed_holidays(self):
        self.assertEqual(previous_session(date(2026, 7, 5)), date(2026, 7, 2))
        self.assertEqual(previous_session(date(2026, 10, 25)), date(2026, 10, 23))
