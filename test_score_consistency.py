"""Exercise the real SQLite path shared by the leaderboard and weekly grid."""

from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database
from score_service import update_scores


class GameDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 21)


class ScoreConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(database, "DATABASE_PATH", Path(self.directory.name) / "test.db")
        self.path_patch.start()
        self.date_patch = patch("score_service.date", GameDate)
        self.date_patch.start()
        self.week = date(2026, 10, 19)
        database.initialize_database()
        connection = database.connect()
        try:
            with connection:
                connection.execute("INSERT INTO fantasy_teams (id, name) VALUES (1, 'Test Team')")
                connection.execute("""INSERT INTO players
                    (player_id, nba_player_id, source_key, full_name, position, nba_team_name)
                    VALUES (1, 123, 'test', 'Test Player', 'G', 'Boston Celtics')""")
                connection.execute("""INSERT INTO weekly_active_rosters
                    (week_start, fantasy_team_id, player_id, slot)
                    VALUES ('2026-10-19', 1, 1, 'G1')""")
        finally:
            connection.close()
        database.save_player_season_logs(1, "2026-27", [
            {"game_date": "2026-10-19", "points": 30, "matchup": "TEST"},
            {"game_date": "2026-10-21", "points": 10, "matchup": "TEST"},
        ])

    def tearDown(self):
        self.date_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    def refresh(self, points=None, status="Live", error=None):
        live = {} if points is None else {
            123: {"game_date": date(2026, 10, 21), "points": points, "status": status}
        }
        with patch("score_service.get_live_player_points", return_value=(live, error)):
            return update_scores(1, self.week, refresh_logs=False)

    def assert_totals(self, expected):
        scores, games = database.get_week_score_snapshot(1, self.week)
        leaderboard = database.get_leaderboard(self.week, date(2026, 10, 21))
        self.assertEqual(sum(score["points"] for score in scores), expected)
        self.assertEqual(sum(game["points"] for game in games), expected)
        self.assertEqual(leaderboard[0]["current_week_points"], expected)
        return games

    def test_live_totals_survive_feed_failure_and_database_reopen(self):
        self.refresh(18)
        games = self.assert_totals(48)
        self.assertEqual([g["status"] for g in games if g["game_date"] == "2026-10-21"], ["Live"])
        # A transient live failure must not erase an already observed game.
        _, warnings = self.refresh(error="timeout")
        self.assertTrue(warnings)
        self.assert_totals(48)
        database.initialize_database()
        self.assert_totals(48)
        self.refresh(20, "Final")
        self.assert_totals(50)
        self.refresh()
        self.assert_totals(50)

    def test_failed_breakdown_write_rolls_back_totals_together(self):
        self.refresh(18)
        with self.assertRaises(sqlite3.IntegrityError):
            database.replace_week_player_scores(1, self.week, [{
                "player_id": 1, "slot": "G1", "points": 999, "status": "ok",
                "games": [{"game_date": "2026-10-21", "points": None}],
            }])
        self.assert_totals(48)


if __name__ == "__main__":
    unittest.main()
