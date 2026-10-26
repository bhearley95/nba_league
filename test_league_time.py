import unittest
from datetime import date, datetime, timezone

from league_time import league_today


class LeagueTimeTests(unittest.TestCase):
    def test_utc_monday_is_still_sunday_before_hawaii_midnight(self):
        for hour in (0, 7, 9):
            with self.subTest(utc_hour=hour):
                self.assertEqual(league_today(datetime(2026, 10, 26, hour, 59, tzinfo=timezone.utc)),
                                 date(2026, 10, 25))

    def test_week_rolls_over_at_hawaii_midnight(self):
        self.assertEqual(league_today(datetime(2026, 10, 26, 10, tzinfo=timezone.utc)),
                         date(2026, 10, 26))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            league_today(datetime(2026, 10, 26))
