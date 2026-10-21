from datetime import date, datetime, timedelta, timezone
from threading import Event
import unittest
from unittest.mock import patch

import requests

from free_agent_service import FreeAgentRefresh, _fetch_stats, _fetch_stats_from_boxscores


class FreeAgentServiceTests(unittest.TestCase):
    def test_stats_timeout_uses_nba_boxscore_fallback(self):
        expected = [{"nba_player_id": 123, "points_per_game": 20}]
        with (
            patch("free_agent_service._fetch_stats_from_stats_api", side_effect=requests.ReadTimeout("offline")),
            patch("free_agent_service._fetch_stats_from_boxscores", return_value=expected) as fallback,
        ):
            self.assertEqual(_fetch_stats("2026-27"), expected)
            fallback.assert_called_once_with("2026-27", None, None)

    def test_cached_boxscores_compute_points_per_game_for_selected_period(self):
        games = [
            {"game_id": "0021", "game_date": "2026-10-19", "nba_team_name": "Test"},
            {"game_id": "0022", "game_date": "2026-10-20", "nba_team_name": "Test"},
        ]
        saved = [{**game, "players": [{"nba_player_id": 123, "points": points, "nba_team_name": "Test"}]}
                 for game, points in zip(games, (10, 30))]
        with (
            patch("free_agent_service.get_season_schedule", return_value=games),
            patch("free_agent_service.database.get_completed_boxscores", return_value=saved),
            patch("free_agent_service.requests.get", side_effect=AssertionError("Cached game downloaded again")),
        ):
            total = _fetch_stats_from_boxscores("2026-27", date(2026, 10, 19), date(2026, 10, 20))
            period = _fetch_stats_from_boxscores("2026-27", date(2026, 10, 20), date(2026, 10, 20))
        self.assertEqual(total[0]["points_per_game"], 20)
        self.assertEqual(total[0]["games_played"], 2)
        self.assertEqual(period[0]["points_per_game"], 30)

    def test_slow_refresh_does_not_block_cached_rankings_or_duplicate(self):
        release = Event()
        worker = FreeAgentRefresh()
        leaders = [{"player_id": 1, "points_per_game": 20}]
        stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=4)

        def slow(*args):
            release.wait(timeout=5)
            return []

        try:
            with (
                patch("free_agent_service.database.get_free_agent_rankings", return_value=(leaders, stale)) as read,
                patch("free_agent_service._fetch_stats", side_effect=slow) as fetch,
                patch("free_agent_service.database.save_season_player_stats"),
            ):
                self.assertEqual(worker.poll("2026-27", date(2026, 10, 19)), (leaders, None, True, stale))
                self.assertEqual(worker.poll("2026-27", date(2026, 10, 19))[0], leaders)
                future = next(iter(worker.requests.values()))["future"]
                release.set()
                future.result(timeout=1)
                read.return_value = (leaders, datetime.now(timezone.utc).replace(tzinfo=None))
                self.assertFalse(worker.poll("2026-27", date(2026, 10, 19))[2])
                self.assertEqual(fetch.call_count, 1)
        finally:
            release.set()
            worker.executor.shutdown(wait=True)
