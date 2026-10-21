import unittest
from datetime import date
from threading import Event
from unittest.mock import Mock, patch

from overview_service import OverviewRefresh, refresh_overview_scores
from score_service import HISTORY_WARNING


class OverviewRefreshTests(unittest.TestCase):
    def test_cached_leaderboard_returns_while_cloud_refresh_is_blocked(self):
        from threading import Event
        release = Event()
        shared = Mock()
        shared.poll.return_value = ([], set(), [], False)
        worker = OverviewRefresh(shared)
        week = date(2026, 10, 19)
        rows = [{"id": 1, "name": "A", "current_week_points": 48}]

        def slow_read(*args):
            release.wait(timeout=5)
            return rows

        try:
            with (
                patch("overview_service.database.get_leaderboard", side_effect=slow_read) as read,
                patch("overview_service.database.get_teams", return_value=[]),
            ):
                first = worker.get_snapshot(week, week)
                self.assertIsNone(first[0])
                future = worker.snapshots[(week, week)]["future"]
                self.assertIs(worker.snapshots[(week, week)]["future"], future)
                self.assertIsNone(worker.get_snapshot(week, week)[0])
                release.set()
                future.result(timeout=1)
                self.assertEqual(worker.get_snapshot(week, week)[0], rows)
                self.assertEqual(read.call_count, 1)
                release.clear()
                with patch("overview_service.monotonic", return_value=worker.snapshots[(week, week)]["finished"] + 31):
                    cached = worker.get_snapshot(week, week)
                self.assertEqual(cached[0], rows)
                self.assertTrue(cached[2])
        finally:
            release.set()
            worker.executor.shutdown(wait=True)

    def test_overview_uses_shared_weekly_worker(self):
        shared = Mock()
        shared.poll.return_value = ([], set(), [HISTORY_WARNING], True)
        worker = OverviewRefresh(shared)
        week = date(2026, 10, 19)
        try:
            with (
                patch("overview_service.database.get_teams", return_value=[
                    {"id": 1, "name": "A"}, {"id": 2, "name": "B"}
                ]),
                patch("overview_service.database.get_week_active_roster", return_value=[{}]),
                patch("overview_service.update_scores", side_effect=AssertionError("Separate refresh started")),
            ):
                self.assertEqual(worker.poll(week), ([HISTORY_WARNING], True))
            self.assertEqual(shared.poll.call_count, 2)
        finally:
            worker.executor.shutdown(wait=True)

    def test_slow_refresh_does_not_block_or_duplicate_requests(self):
        release = Event()
        started = Event()

        def slow_refresh(week):
            started.set()
            release.wait(timeout=5)
            return ["NBA data unavailable"]

        worker = OverviewRefresh()
        week = date(2026, 10, 19)
        try:
            with patch("overview_service.refresh_overview_scores", side_effect=slow_refresh) as refresh:
                self.assertEqual(worker.poll(week), ([], True))
                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(worker.poll(week), ([], True))
                self.assertEqual(refresh.call_count, 1)
                future = worker.weeks[week]["future"]
                release.set()
                future.result(timeout=1)
                self.assertEqual(worker.poll(week), (["NBA data unavailable"], False))
                self.assertEqual(worker.poll(week), (["NBA data unavailable"], False))
                self.assertEqual(refresh.call_count, 1)
                with patch("overview_service.monotonic", return_value=worker.weeks[week]["finished"] + 31):
                    self.assertTrue(worker.poll(week)[1])
        finally:
            release.set()
            worker.executor.shutdown(wait=True)

    def test_team_failure_does_not_stop_other_teams(self):
        with (
            patch("overview_service.database.get_teams", return_value=[
                {"id": 1, "name": "A"}, {"id": 2, "name": "B"}
            ]),
            patch("overview_service.database.get_week_active_roster", return_value=[{}]),
            patch("overview_service.update_scores", side_effect=[RuntimeError("offline"), ([], [])]) as update,
        ):
            warnings = refresh_overview_scores(date(2026, 10, 19))
        self.assertEqual(update.call_count, 2)
        self.assertEqual(warnings, ["A: Score refresh failed. offline"])

    def test_history_outage_shown_once_across_teams(self):
        with (
            patch("overview_service.database.get_teams", return_value=[
                {"id": 1, "name": "A"}, {"id": 2, "name": "B"}
            ]),
            patch("overview_service.database.get_week_active_roster", return_value=[{}]),
            patch("overview_service.update_scores", return_value=([], [HISTORY_WARNING])),
        ):
            self.assertEqual(refresh_overview_scores(date(2026, 10, 19)), [HISTORY_WARNING])


if __name__ == "__main__":
    unittest.main()
