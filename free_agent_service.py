from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic
import requests
import re
from zoneinfo import ZoneInfo

from nba_api.stats.endpoints import leaguedashplayerstats
from nba_api.stats.static import teams

import database
from schedule_service import get_season_schedule
from score_service import LIVE_DATA_BASE


STATS_CACHE_TTL = timedelta(hours=3)


def nba_today():
    return datetime.now(ZoneInfo("America/New_York")).date()


def _error_detail(exc):
    message = str(exc)
    if database.CLOUD_URL:
        message = message.replace(database.CLOUD_URL, "[cloud connection]")
    message = re.sub(r"(?i)(apikey|token|password)=([^&\s]+)", r"\1=[hidden]", message)
    return f"{type(exc).__name__}: {message}"


class FreeAgentRefresh:
    """Read cloud rankings immediately; share NBA refreshes between viewers."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="free-agents")
        self.lock = Lock()
        self.requests = {}

    @staticmethod
    def _refresh(season, period_start, period_end):
        stats = _fetch_stats(season, period_start, period_end)
        try:
            if period_start is not None:
                database.save_player_period_stats(season, period_start, period_end, stats)
            else:
                database.save_season_player_stats(season, stats)
        except Exception as exc:
            raise RuntimeError(f"Could not save scoring stats to league storage. {_error_detail(exc)}") from None

    def poll(self, season, week_start, period_start=None, period_end=None, refresh=False):
        key = (season, period_start, period_end)
        with self.lock:
            state = self.requests.setdefault(key, {"future": None, "retry_at": 0, "warning": None})
            future = state["future"]
            if future is not None and future.done():
                try:
                    future.result()
                    state["warning"] = None
                    state["retry_at"] = 0
                except Exception as exc:
                    state["warning"] = f"Scoring refresh failed. {_error_detail(exc)}. Showing saved rankings; retrying in five minutes."
                    state["retry_at"] = monotonic() + 300
                state["future"] = None
            leaders, fetched_at = database.get_free_agent_rankings(season, week_start, period_start, period_end)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            stale = fetched_at is None or now - fetched_at >= STATS_CACHE_TTL
            if state["future"] is None and (refresh or (stale and monotonic() >= state["retry_at"])):
                state["future"] = self.executor.submit(self._refresh, season, period_start, period_end)
            return leaders, state["warning"], state["future"] is not None, fetched_at


def _fetch_stats_from_stats_api(
    season: str, period_start: date | None = None, period_end: date | None = None
) -> list[dict]:
    parameters = {
        "season": season,
        "season_type_all_star": "Regular Season",
        "per_mode_detailed": "PerGame",
        "measure_type_detailed_defense": "Base",
        "timeout": 8,
    }
    if period_start and period_end:
        parameters["date_from_nullable"] = period_start.strftime("%m/%d/%Y")
        parameters["date_to_nullable"] = period_end.strftime("%m/%d/%Y")

    frame = leaguedashplayerstats.LeagueDashPlayerStats(**parameters).get_data_frames()[0]
    full_name_by_abbreviation = {
        team["abbreviation"]: team["full_name"] for team in teams.get_teams()
    }
    rows_by_player = {}
    for _, row in frame.iterrows():
        games_played = int(row["GP"] or 0)
        if games_played <= 0:
            continue
        nba_player_id = int(row["PLAYER_ID"])
        abbreviation = str(row["TEAM_ABBREVIATION"] or "")
        player_stats = {
            "nba_player_id": nba_player_id,
            "games_played": games_played,
            "points_per_game": float(row["PTS"] or 0),
            "nba_team_name": full_name_by_abbreviation.get(
                abbreviation, abbreviation
            ),
        }
        previous = rows_by_player.get(nba_player_id)
        # For traded players, prefer the API's combined TOT row; otherwise keep
        # the row covering the most games.
        if previous is None or abbreviation == "TOT" or games_played > previous["games_played"]:
            rows_by_player[nba_player_id] = player_stats
    return list(rows_by_player.values())


def _fetch_completed_boxscore(game):
    response = requests.get(f"{LIVE_DATA_BASE}/boxscore/boxscore_{game['game_id']}.json", timeout=10)
    response.raise_for_status()
    box = response.json()["game"]
    if int(box.get("gameStatus", 0)) != 3:
        return None
    players = []
    for side in ("homeTeam", "awayTeam"):
        team = box[side]
        for player in team.get("players", []):
            if str(player.get("played", "0")) != "1":
                continue
            players.append({"nba_player_id": int(player["personId"]),
                            "points": int(player.get("statistics", {}).get("points", 0) or 0),
                            "nba_team_name": f"{team['teamCity']} {team['teamName']}"})
    return {"game_id": game["game_id"], "game_date": game["game_date"], "players": players}


def _fetch_stats_from_boxscores(season, period_start=None, period_end=None):
    today = nba_today()
    start = period_start.isoformat() if period_start else f"{season[:4]}-10-01"
    end = min(period_end or today, today).isoformat()
    games = {game["game_id"]: game for game in get_season_schedule(season)
             if start <= game["game_date"] <= end}
    saved = {game["game_id"]: game for game in database.get_completed_boxscores(season)
             if game["game_id"] in games}
    missing = [game for game_id, game in games.items() if game_id not in saved]
    if any(game["game_date"] == today.isoformat() for game in missing):
        response = requests.get(f"{LIVE_DATA_BASE}/scoreboard/todaysScoreboard_00.json", timeout=10)
        response.raise_for_status()
        scoreboard = response.json()["scoreboard"]
        final_ids = {
            str(game["gameId"]) for game in scoreboard.get("games", [])
            if int(game.get("gameStatus", 0)) == 3
        } if scoreboard["gameDate"] == today.isoformat() else set()
        missing = [game for game in missing
                   if game["game_date"] < today.isoformat() or game["game_id"] in final_ids]
    completed = []
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            for game in executor.map(_fetch_completed_boxscore, missing):
                if game is not None:
                    completed.append(game)
    finally:
        # Retain successful downloads even if another game request fails.
        if completed:
            database.save_completed_boxscores(season, completed)
    saved.update({game["game_id"]: game for game in completed})
    totals = {}
    for game in saved.values():
        for player in game["players"]:
            row = totals.setdefault(player["nba_player_id"], {
                "nba_player_id": player["nba_player_id"], "games_played": 0,
                "points": 0, "nba_team_name": player["nba_team_name"],
            })
            row["games_played"] += 1
            row["points"] += player["points"]
    return [{"nba_player_id": row["nba_player_id"], "games_played": row["games_played"],
             "points_per_game": row["points"] / row["games_played"], "nba_team_name": row["nba_team_name"]}
            for row in totals.values()]


def _fetch_stats(season, period_start=None, period_end=None):
    try:
        return _fetch_stats_from_stats_api(season, period_start, period_end)
    except (requests.RequestException, ValueError, KeyError, IndexError):
        try:
            return _fetch_stats_from_boxscores(season, period_start, period_end)
        except Exception as exc:
            raise RuntimeError(f"Could not load NBA completed-game scoring data. {_error_detail(exc)}") from None


def get_scoring_leaders(
    season: str,
    refresh: bool = False,
    period_start: date | None = None,
    period_end: date | None = None,
) -> tuple[list[dict], str | None]:
    """Return cached or freshly fetched regular-season points-per-game leaders."""
    if (period_start is None) != (period_end is None):
        raise ValueError("Both period start and end dates are required.")

    if period_start and period_end:
        fetched_at = database.get_player_period_stats_sync_time(
            season, period_start, period_end
        )
        get_cached = lambda: database.get_player_period_stats(
            season, period_start, period_end
        )
        save_stats = lambda stats: database.save_player_period_stats(
            season, period_start, period_end, stats
        )
    else:
        fetched_at = database.get_player_stats_sync_time(season)
        get_cached = lambda: database.get_season_player_stats(season)
        save_stats = lambda stats: database.save_season_player_stats(season, stats)

    # SQLite CURRENT_TIMESTAMP is UTC and stored without timezone metadata.
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    is_fresh = fetched_at and utc_now - fetched_at < STATS_CACHE_TTL
    warning = None

    if refresh or not is_fresh:
        try:
            save_stats(_fetch_stats(season, period_start, period_end))
        except Exception as exc:
            warning = f"Could not refresh NBA scoring leaders: {exc}"
            if fetched_at is None:
                return [], warning

    leaders = [dict(row) for row in get_cached()]
    return leaders, warning
