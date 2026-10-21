import unittest
from unittest.mock import Mock, patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from score_service import HISTORY_WARNING
from schedule_service import get_week_schedule
import test_score_consistency as fixtures


class ScorePageTests(unittest.TestCase):
    def test_overview_and_weekly_grid_use_same_saved_total(self):
        fixture = fixtures.ScoreConsistencyTests()
        fixture.setUp()
        st.cache_resource.clear()
        try:
            fixture.refresh(18)
            shared = Mock()
            shared.poll.return_value = ([], get_week_schedule(fixture.week), [HISTORY_WARNING], False)
            with (
                patch("weekly_score_service.WeeklyScoreRefresh", return_value=shared),
                patch("database.sync_player_catalog", return_value=1),
            ):
                app = AppTest.from_file("app.py", default_timeout=15).run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(int(app.dataframe[0].value.iloc[0]["This Week"]), 48)
                self.assertEqual(len(app.warning), 0)
                app.button(key="nav_weekly_scores").click().run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(app.metric[0].value, "48")
                self.assertEqual(str(app.dataframe[0].value.iloc[-1]["Test Player"]), "48")
                self.assertIn("#dbeafe", str(app.dataframe[0].proto))
                self.assertEqual(len(app.warning), 0)
                self.assertTrue(any("Historical game logs are delayed" in caption.value for caption in app.caption))
                with patch("database.get_nba_season_schedule", side_effect=AssertionError("Schedule queried SQL")):
                    app.button(key="nav_schedule_grid").click().run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(len(app.error), 0)
                self.assertEqual(len(app.dataframe[0].value), 30)
        finally:
            st.cache_resource.clear()
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
