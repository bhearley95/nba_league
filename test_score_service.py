import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pandas as pd
import requests

from score_service import HISTORY_WARNING, _refresh_player_log, refresh_player_logs_for_week, update_scores


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
            patch("score_service.database.get_week_score_snapshot", return_value=([], [])),
            patch("score_service.database.get_team_roster", return_value=[]),
            patch("score_service.database.save_roster_game_points"),
            patch("score_service.database.get_player_week_games", return_value=[
                {"game_date": (today - timedelta(days=1)).isoformat(), "points": 30, "status": "Historical"},
                {"game_date": today.isoformat(), "points": 10, "status": "Historical"},
            ]),
            patch("score_service.database.replace_week_player_scores") as save,
        ):
            scores, warnings = update_scores(1, week, {"G1": player}, refresh_logs=False)
        history.assert_not_called()
        self.assertEqual(warnings, [])
        self.assertEqual(scores[0]["points"], 48)
        self.assertEqual(scores[0]["live_points"], 18)
        self.assertEqual(scores[0]["live_status"], "Live")
        save.assert_called_once_with(1, week, scores)


class HistoryRetryTests(unittest.TestCase):
    def test_timeout_pauses_all_history_requests_then_retries(self):
        player = {"player_id": 1, "nba_player_id": 123, "full_name": "Test Player"}
        stale = datetime.now() - timedelta(hours=2)
        with (
            patch("score_service._history_retry_at", 0),
            patch("score_service.monotonic", return_value=100) as clock,
            patch("score_service.database.get_player_log_sync_time", return_value=stale),
            patch("score_service.database.save_player_season_logs") as save,
            patch("score_service.playergamelog.PlayerGameLog", side_effect=requests.ReadTimeout("offline")) as fetch,
            self.assertLogs("score_service", level="WARNING"),
        ):
            error, status = _refresh_player_log(player, "2026-27")
            self.assertEqual(error, HISTORY_WARNING)
            self.assertIn("cached", status.lower())
            clock.return_value = 200
            warnings = refresh_player_logs_for_week([
                player, {**player, "player_id": 2, "nba_player_id": 456}
            ], date(2026, 10, 19))
            self.assertEqual(warnings, [HISTORY_WARNING])
            self.assertEqual(fetch.call_count, 1)
            save.assert_not_called()
            clock.return_value = 401
            fetch.side_effect = None
            fetch.return_value.get_data_frames.return_value = [pd.DataFrame()]
            self.assertEqual(_refresh_player_log(player, "2026-27"), (None, "Updated"))
            self.assertEqual(fetch.call_count, 2)
            save.assert_called_once_with(1, "2026-27", [])


if __name__ == "__main__":
    unittest.main()
