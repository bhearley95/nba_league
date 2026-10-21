"""Refresh overview scores without blocking Streamlit's rendering thread."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
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

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="overview")
        self.lock = Lock()
        self.weeks = {}

    def poll(self, week_start: date) -> tuple[list[str], bool]:
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
