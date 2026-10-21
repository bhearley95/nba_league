"""Refresh overview scores without blocking Streamlit's rendering thread."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Lock
from time import monotonic

import database
from score_service import HISTORY_WARNING, update_scores


def refresh_overview_scores(week_start: date) -> list[str]:
    warnings = []
    for team in database.get_teams():
        if database.get_week_active_roster(team["id"], week_start):
            try:
                _, team_warnings = update_scores(team["id"], week_start)
                for warning in team_warnings:
                    if warning == HISTORY_WARNING:
                        if warning not in warnings:
                            warnings.append(warning)
                    else:
                        warnings.append(f"{team['name']}: {warning}")
            except Exception as exc:
                warnings.append(f"{team['name']}: Score refresh failed. {exc}")
    return warnings


class OverviewRefresh:
    """Share one worker across viewers and throttle retries after completion."""

    def __init__(self, score_refresh=None):
        self.score_refresh = score_refresh
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="overview")
        self.lock = Lock()
        self.weeks = {}
        self.snapshots = {}

    def _load_snapshot(self, key):
        week_start, as_of = key
        leaderboard = [dict(row) for row in database.get_leaderboard(week_start, as_of)]
        # Publish saved standings before scheduling each team's score refresh.
        with self.lock:
            self.snapshots[key]["leaderboard"] = leaderboard
            self.snapshots[key]["updated_at"] = datetime.now(timezone.utc)
        warnings, _ = self.poll(week_start)
        return warnings

    def get_snapshot(self, week_start, as_of):
        """Return memory-cached standings immediately; do all cloud work off-thread."""
        key = (week_start, as_of)
        with self.lock:
            state = self.snapshots.setdefault(key, {
                "leaderboard": None, "updated_at": None, "warnings": [],
                "future": None, "finished": None,
            })
            future = state["future"]
            if future is not None and future.done():
                try:
                    state["warnings"] = future.result()
                except Exception:
                    state["warnings"] = ["Could not update the leaderboard. Showing the last loaded standings."]
                state["future"] = None
                state["finished"] = monotonic()
            if state["future"] is None and (
                state["finished"] is None or monotonic() - state["finished"] >= 30
            ):
                state["future"] = self.executor.submit(self._load_snapshot, key)
            return state["leaderboard"], list(state["warnings"]), state["future"] is not None, state["updated_at"]

    def poll(self, week_start: date) -> tuple[list[str], bool]:
        if self.score_refresh is not None:
            warnings = []
            refreshing = False
            for team in database.get_teams():
                if database.get_week_active_roster(team["id"], week_start):
                    _, _, team_warnings, pending = self.score_refresh.poll(team["id"], week_start)
                    warnings.extend(team_warnings)
                    refreshing = refreshing or pending
            return list(dict.fromkeys(warnings)), refreshing
        with self.lock:
            state = self.weeks.setdefault(
                week_start, {"future": None, "finished": None, "warnings": []}
            )
            future = state["future"]
            if future is not None and future.done():
                try:
                    state["warnings"] = future.result()
                except Exception as exc:
                    state["warnings"] = [f"Score refresh failed. {exc}"]
                state["future"] = None
                state["finished"] = monotonic()
            if state["future"] is None and (
                state["finished"] is None or monotonic() - state["finished"] >= 30
            ):
                state["future"] = self.executor.submit(refresh_overview_scores, week_start)
            return list(state["warnings"]), state["future"] is not None
