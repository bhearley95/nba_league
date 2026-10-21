import unittest
from datetime import date, timedelta
from unittest.mock import patch

from score_service import update_scores


class LiveScoreTests(unittest.TestCase):
    def test_live_only_update_skips_history_and_replaces_existing_game_points(self):
        today = date.today()
        week = today - timedelta(days=today.weekday())
        player = {"player_id": 1, "nba_player_id": 123, "full_name": "Test Player"}
        with (
            patch("score_service.get_live_player_points", return_value=({
                123: {"game_date": today, "points": 18, "status": "Live"}
            }, None)),
            patch("score_service._refresh_player_log", side_effect=AssertionError("History must not block live scores")) as history,
            patch("score_service.database.get_player_week_points", return_value=40),
            patch("score_service.database.get_player_game_points_on_date", return_value=10),
            patch("score_service.database.replace_week_player_scores") as save,
        ):
            scores, warnings = update_scores(1, week, {"G1": player}, refresh_logs=False)
        history.assert_not_called()
        self.assertEqual(warnings, [])
        self.assertEqual(scores[0]["points"], 48)
        self.assertEqual(scores[0]["live_points"], 18)
        self.assertEqual(scores[0]["live_status"], "Live")
        save.assert_called_once_with(1, week, scores)


if __name__ == "__main__":
    unittest.main()
