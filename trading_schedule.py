"""New York opening window and regular US equity exchange holidays."""

from datetime import date, time, timedelta

from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay, USMartinLutherKingJr,
    USMemorialDay, USPresidentsDay, USThanksgivingDay, nearest_workday, sunday_to_monday,
)


class USStockHolidays(AbstractHolidayCalendar):
    rules = [
        Holiday("New Year", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay, USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
        Holiday("Carter funeral", year=2025, month=1, day=9),
    ]


def is_session(day: date) -> bool:
    return day.weekday() < 5 and USStockHolidays().holidays(day, day).empty


def opening_window(day: date, local_time: time) -> bool:
    return is_session(day) and time(9, 30) <= local_time < time(17, 30)


def previous_session(day: date) -> date:
    day -= timedelta(days=1)
    while not is_session(day):
        day -= timedelta(days=1)
    return day
