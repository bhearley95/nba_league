import unittest
from datetime import date
from threading import Event
from unittest.mock import patch

from weekly_score_service import WeeklyScoreRefresh


class WeeklyScoreRefreshTests(unittest.TestCase):
    def setUp(self):
        self.worker = WeeklyScoreRefresh()
        self.week = date(2026, 10, 19)
        self.release = Event()
        self.schedule_patch = patch(
            "weekly_score_service.database.get_nba_season_schedule", side_effect=AssertionError("Schedule must not query SQL")
        )
        self.schedule_patch.start()
        self.history_patch = patch("weekly_score_service.refresh_player_logs_for_week", return_value=[])
        self.roster_patch = patch("weekly_score_service.database.get_team_roster", return_value=[])
        self.history_patch.start()
        self.roster_patch.start()

    def tearDown(self):
        self.release.set()
        self.worker.executor.shutdown(wait=True)
        self.worker.history_executor.shutdown(wait=True)
        self.schedule_patch.stop()
        self.history_patch.stop()
        self.roster_patch.stop()

    def finish_requests(self):
        for state in list(self.worker.requests.values()):
            if state["future"]:
                state["future"].result(timeout=1)

    def test_static_schedule_available_while_scores_refresh(self):
        def scores(*args, **kwargs):
            self.release.wait(timeout=5)
            return [{"player_id": 1, "live_points": 12}], []

        def schedule(*args):
            return {(self.week, "Boston Celtics")}

        with (
            patch("weekly_score_service.update_scores", side_effect=scores) as update,
            patch("weekly_score_service.get_week_schedule", side_effect=schedule) as fetch_schedule,
        ):
            self.assertEqual(self.worker.poll(1, self.week), ([], {(self.week, "Boston Celtics")}, [], True))
            self.assertEqual(self.worker.poll(1, self.week), ([], {(self.week, "Boston Celtics")}, [], True))
            self.release.set()
            self.finish_requests()
            result = self.worker.poll(1, self.week)
            self.assertEqual(result, (
                [{"player_id": 1, "live_points": 12}], {(self.week, "Boston Celtics")}, [], False
            ))
            self.assertEqual(update.call_count, 1)
            self.assertGreaterEqual(fetch_schedule.call_count, 1)

    def test_schedule_shared_but_team_scores_kept_separate(self):
        with (
            patch("weekly_score_service.update_scores", side_effect=lambda team, week, **kwargs: ([{"player_id": team}], [])),
            patch("weekly_score_service.get_week_schedule", return_value=set()) as schedule,
        ):
            self.worker.poll(1, self.week)
            self.worker.poll(2, self.week)
            self.finish_requests()
            self.assertEqual(self.worker.poll(1, self.week)[0], [{"player_id": 1}])
            self.assertEqual(self.worker.poll(2, self.week)[0], [{"player_id": 2}])
            self.assertGreaterEqual(schedule.call_count, 1)

    def test_schedule_failure_preserves_saved_schedule_and_scores(self):
        self.worker.schedules[self.week] = {(self.week, "Boston Celtics")}
        with (
            patch("weekly_score_service.database.get_nba_season_schedule", return_value=[
                {"game_date": self.week.isoformat(), "nba_team_name": "Boston Celtics"}
            ]),
            patch("weekly_score_service.update_scores", return_value=([{"player_id": 1}], [])),
            patch("weekly_score_service.get_week_schedule", side_effect=RuntimeError("offline")),
        ):
            self.assertEqual(self.worker.poll(1, self.week)[1], {(self.week, "Boston Celtics")})
            for state in self.worker.requests.values():
                try:
                    state["future"].result(timeout=1)
                except RuntimeError:
                    pass
            scores, schedule, warnings, refreshing = self.worker.poll(1, self.week)
            self.assertEqual(scores, [{"player_id": 1}])
            self.assertEqual(schedule, {(self.week, "Boston Celtics")})
            self.assertIn("offline", warnings[0])
            self.assertFalse(refreshing)

    def test_live_scores_available_while_history_is_still_blocked(self):
        history_started = Event()

        def slow_history(*args):
            history_started.set()
            self.release.wait(timeout=5)
            return []

        live = [{"player_id": 1, "live_points": 18, "live_status": "Live"}]
        with (
            patch("weekly_score_service.update_scores", return_value=(live, [])) as update,
            patch("weekly_score_service.get_week_schedule", return_value=set()),
            patch("weekly_score_service.refresh_player_logs_for_week", side_effect=slow_history),
        ):
            self.worker.poll(1, self.week)
            self.assertTrue(history_started.wait(timeout=1))
            self.worker.requests[("scores", 1, self.week)]["future"].result(timeout=1)
            self.assertEqual(self.worker.poll(1, self.week)[0], live)
            self.assertFalse(self.worker.requests[("game logs", 1, self.week)]["future"].done())
            update.assert_called_once_with(1, self.week, refresh_logs=False)
            self.release.set()
            self.finish_requests()


if __name__ == "__main__":
    unittest.main()
