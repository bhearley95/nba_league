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
            "weekly_score_service.database.get_nba_season_schedule", return_value=[]
        )
        self.schedule_patch.start()

    def tearDown(self):
        self.release.set()
        self.worker.executor.shutdown(wait=True)
        self.schedule_patch.stop()

    def finish_requests(self):
        for state in list(self.worker.requests.values()):
            if state["future"]:
                state["future"].result(timeout=1)

    def test_slow_scores_and_schedule_do_not_block_or_duplicate(self):
        def scores(*args):
            self.release.wait(timeout=5)
            return [{"player_id": 1, "live_points": 12}], []

        def schedule(*args):
            self.release.wait(timeout=5)
            return {(self.week, "Boston Celtics")}

        with (
            patch("weekly_score_service.update_scores", side_effect=scores) as update,
            patch("weekly_score_service.get_week_schedule", side_effect=schedule) as fetch_schedule,
        ):
            self.assertEqual(self.worker.poll(1, self.week), ([], set(), [], True))
            self.assertEqual(self.worker.poll(1, self.week), ([], set(), [], True))
            self.release.set()
            self.finish_requests()
            result = self.worker.poll(1, self.week)
            self.assertEqual(result, (
                [{"player_id": 1, "live_points": 12}], {(self.week, "Boston Celtics")}, [], False
            ))
            self.assertEqual(update.call_count, 1)
            self.assertEqual(fetch_schedule.call_count, 1)

    def test_schedule_shared_but_team_scores_kept_separate(self):
        with (
            patch("weekly_score_service.update_scores", side_effect=lambda team, week: ([{"player_id": team}], [])),
            patch("weekly_score_service.get_week_schedule", return_value=set()) as schedule,
        ):
            self.worker.poll(1, self.week)
            self.worker.poll(2, self.week)
            self.finish_requests()
            self.assertEqual(self.worker.poll(1, self.week)[0], [{"player_id": 1}])
            self.assertEqual(self.worker.poll(2, self.week)[0], [{"player_id": 2}])
            self.assertEqual(schedule.call_count, 1)

    def test_schedule_failure_preserves_saved_schedule_and_scores(self):
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


if __name__ == "__main__":
    unittest.main()
