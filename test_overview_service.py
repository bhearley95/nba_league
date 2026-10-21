import unittest
from datetime import date
from threading import Event
from unittest.mock import patch

from overview_service import OverviewRefresh, refresh_overview_scores


class OverviewRefreshTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
