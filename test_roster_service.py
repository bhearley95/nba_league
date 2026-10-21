from datetime import date
from threading import Event
import unittest
from unittest.mock import Mock, patch

from roster_service import RosterSnapshots


class RosterCacheTests(unittest.TestCase):
    def test_cached_rosters_display_during_slow_cloud_reads(self):
        release = Event()
        shared = Mock()
        shared.poll.return_value = ([], set(), [], False)
        worker = RosterSnapshots(shared)
        week = date(2026, 10, 19)
        teams = [{"id": 1, "roster": [{"player_id": 1}], "points": {1: 22}}]

        def load(*args):
            release.wait(timeout=5)
            return teams

        try:
            with patch("roster_service.load_roster_snapshot", side_effect=load) as read:
                self.assertIsNone(worker.poll(week)[0])
                first = worker.weeks[week]["future"]
                worker.poll(week)
                self.assertIs(worker.weeks[week]["future"], first)
                release.set()
                first.result(timeout=1)
                self.assertEqual(worker.poll(week)[0], teams)
                self.assertEqual(read.call_count, 1)
                release.clear()
                worker.invalidate(week)
                cached, _, updating = worker.poll(week)
                self.assertEqual(cached, teams)
                self.assertTrue(updating)
        finally:
            release.set()
            worker.executor.shutdown(wait=True)

    def test_failed_refresh_preserves_previous_team_data(self):
        shared = Mock()
        shared.poll.return_value = ([], set(), [], False)
        worker = RosterSnapshots(shared)
        week = date(2026, 10, 19)
        teams = [{"id": 1}]
        try:
            with patch("roster_service.load_roster_snapshot", return_value=teams):
                worker.poll(week)
                worker.weeks[week]["future"].result(timeout=1)
                worker.poll(week)
            worker.invalidate(week)
            with patch("roster_service.load_roster_snapshot", side_effect=RuntimeError("offline")):
                worker.poll(week)
                try:
                    worker.weeks[week]["future"].result(timeout=1)
                except RuntimeError:
                    pass
                cached, warnings, _ = worker.poll(week)
                self.assertEqual(cached, teams)
                self.assertIn("last loaded rosters", warnings[0])
        finally:
            worker.executor.shutdown(wait=True)
