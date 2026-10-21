from datetime import date
import unittest
from unittest.mock import patch

from schedule_service import _read_schedule, get_season_schedule, get_week_schedule


class StaticScheduleTests(unittest.TestCase):
    def test_bundled_schedule_does_not_use_network_or_database(self):
        _read_schedule.cache_clear()
        with (
            patch("database.connect", side_effect=AssertionError("SQL queried")),
            patch("requests.sessions.Session.request", side_effect=AssertionError("Network requested")),
        ):
            season = get_season_schedule("2026-27")
            week = get_week_schedule(date(2026, 10, 19))
        self.assertEqual(len(season), 2400)
        self.assertEqual(len({g["nba_team_name"] for g in season}), 30)
        self.assertTrue(week)
        self.assertTrue(all(date(2026, 10, 19) <= day <= date(2026, 10, 25) for day, _ in week))
        self.assertEqual(week, {
            (date.fromisoformat(g["game_date"]), g["nba_team_name"])
            for g in season if "2026-10-19" <= g["game_date"] <= "2026-10-25"
        })

    def test_missing_season_reports_static_file_error(self):
        with self.assertRaisesRegex(ValueError, "missing from the deployment"):
            get_season_schedule("2099-00")


if __name__ == "__main__":
    unittest.main()
