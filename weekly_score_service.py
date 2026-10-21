"""Share background score and schedule requests between Weekly Scores viewers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Lock
from time import monotonic

import database
from score_service import get_week_schedule, season_for_week, update_scores


class WeeklyScoreRefresh:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="weekly-scores")
        self.lock = Lock()
        self.requests = {}

    def _poll(self, key, fetch, empty, interval):
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
            state["future"] = self.executor.submit(fetch)
        return state["value"], list(state["warnings"]), state["future"] is not None

    def poll(self, team_id: int, week_start: date):
        with self.lock:
            saved_schedule = set()
            if ("schedule", week_start) not in self.requests:
                saved_schedule = {
                    (date.fromisoformat(game["game_date"]), game["nba_team_name"])
                    for game in database.get_nba_season_schedule(season_for_week(week_start))
                    if 0 <= (date.fromisoformat(game["game_date"]) - week_start).days < 7
                }
            scores, score_warnings, scoring = self._poll(
                ("scores", team_id, week_start),
                lambda: update_scores(team_id, week_start), [], 30,
            )
            schedule, schedule_warnings, scheduling = self._poll(
                ("schedule", week_start),
                lambda: (get_week_schedule(week_start), []), saved_schedule, 300,
            )
        return scores, schedule, score_warnings + schedule_warnings, scoring or scheduling
