"""Share background score and schedule requests between Weekly Scores viewers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Lock
from time import monotonic

import database
from score_service import get_week_schedule, refresh_roster_game_logs as refresh_player_logs_for_week, update_scores


class WeeklyScoreRefresh:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="weekly-scores")
        self.history_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="weekly-history")
        self.lock = Lock()
        self.requests = {}
        self.schedules = {}

    def _poll(self, key, fetch, empty, interval, executor=None):
        # Called with the lock held. Never wait for unfinished network requests.
        state = self.requests.setdefault(
            key, {"future": None, "finished": None, "value": empty, "warnings": []}
        )
        future = state["future"]
        if future is not None and future.done():
            try:
                state["value"], state["warnings"] = future.result()
            except Exception as exc:
                # Keep the last successful result available during an outage.
                state["warnings"] = [f"Could not refresh {key[0]}; showing saved data. {exc}"]
            state["finished"] = monotonic()
            state["future"] = None
        if state["future"] is None and (
            state["finished"] is None or monotonic() - state["finished"] >= interval
        ):
            state["future"] = (executor or self.executor).submit(fetch)
        return state["value"], list(state["warnings"]), state["future"] is not None

    def poll(self, team_id: int, week_start: date):
        with self.lock:
            schedule_warnings = []
            try:
                self.schedules[week_start] = get_week_schedule(week_start)
            except Exception as exc:
                schedule_warnings = [f"Could not load the saved NBA schedule. {exc}"]
            schedule = self.schedules.get(week_start, set())
            scores, score_warnings, scoring = self._poll(
                ("scores", team_id, week_start),
                lambda: update_scores(team_id, week_start, refresh_logs=False), [], 30,
            )
            _, history_warnings, _ = self._poll(
                ("game logs", team_id, week_start),
                lambda: (None, refresh_player_logs_for_week(
                    [dict(row) for row in database.get_team_roster(team_id, week_start)],
                    week_start,
                )), None, 300, self.history_executor,
            )
        return scores, schedule, score_warnings + schedule_warnings + history_warnings, scoring
