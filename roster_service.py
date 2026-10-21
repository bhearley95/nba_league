"""Cache roster page data in memory while cloud reads run in the background."""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic

import database
from score_service import season_for_week


def load_roster_snapshot(week_start):
    def load_team(team):
        team = dict(team)
        roster = [dict(row) for row in database.get_team_roster(team["id"], week_start)]
        active = [dict(row) for row in database.get_week_active_roster(team["id"], week_start)]
        scores, _ = database.get_week_score_snapshot(team["id"], week_start)
        points = database.get_players_week_totals([p["player_id"] for p in roster], season_for_week(week_start), week_start)
        points.update({p["player_id"]: p["points"] for p in scores})
        return {**team, "roster": roster, "active": active, "scores": scores, "points": points}

    teams = database.get_teams()
    with ThreadPoolExecutor(max_workers=4) as executor:
        return list(executor.map(load_team, teams))


class RosterSnapshots:
    def __init__(self, score_refresh):
        self.score_refresh = score_refresh
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="roster-cache")
        self.lock = Lock()
        self.weeks = {}

    def _load(self, week):
        teams = load_roster_snapshot(week)
        warnings = []
        for team in teams:
            _, _, messages, _ = self.score_refresh.poll(team["id"], week)
            warnings.extend(messages)
        return teams, list(dict.fromkeys(warnings))

    def poll(self, week):
        with self.lock:
            state = self.weeks.setdefault(week, {"teams": None, "warnings": [], "future": None, "finished": None})
            future = state["future"]
            if future is not None and future.done():
                try:
                    state["teams"], state["warnings"] = future.result()
                except Exception:
                    state["warnings"] = ["Could not refresh roster data. Showing the last loaded rosters."]
                state["future"] = None
                state["finished"] = monotonic()
            if state["future"] is None and (state["finished"] is None or monotonic() - state["finished"] >= 30):
                state["future"] = self.executor.submit(self._load, week)
            return state["teams"], list(state["warnings"]), state["future"] is not None

    def invalidate(self, week):
        with self.lock:
            state = self.weeks.get(week)
            if state:
                # Detach an older read so it cannot replace data after a save.
                state["future"] = None
                state["finished"] = None
