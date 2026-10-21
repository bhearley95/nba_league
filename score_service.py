"""Fetch and recalculate weekly points from cached NBA player game logs."""

from datetime import date, datetime, timedelta
import logging
from threading import Lock
from time import monotonic

import requests
from nba_api.stats.endpoints import playergamelog
from schedule_service import get_season_schedule, get_week_schedule

import database
from nba_api.stats.static import teams as nba_teams


LOG_CACHE_TTL = timedelta(hours=1)
HISTORY_RETRY_SECONDS = 300
HISTORY_WARNING = (
    "Historical NBA game logs are temporarily unavailable. Showing saved game logs; "
    "historical totals may be out of date. Live scores use a separate feed."
)
_history_lock = Lock()
_history_retry_at = 0.0
_logger = logging.getLogger(__name__)
LIVE_DATA_BASE = "https://nba-prod-us-east-1-mediaops-stats.s3.amazonaws.com/NBA/liveData"


def season_for_week(week_start: date) -> str:
    """Return the NBA regular-season label containing a scoring week."""
    start_year = week_start.year if week_start.month >= 10 else week_start.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def normalize_nba_team_name(name: str) -> str:
    """Normalize spacing and common NBA city abbreviations for team matching."""
    key = "".join(character for character in name.casefold() if character.isalnum())
    return {
        "lalakers": "losangeleslakers",
        "laclippers": "losangelesclippers",
    }.get(key, key)


def get_live_player_points(
    players: list[dict],
) -> tuple[dict[int, dict], str | None]:
    """Fetch today's live/final boxscore points for selected NBA players."""
    nba_player_ids = {
        int(player["nba_player_id"])
        for player in players
        if player.get("nba_player_id") is not None
    }
    if not nba_player_ids:
        return {}, None

    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}
    try:
        response = session.get(
            f"{LIVE_DATA_BASE}/scoreboard/todaysScoreboard_00.json",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        scoreboard = response.json().get("scoreboard", {})
        game_date = date.fromisoformat(scoreboard["gameDate"])
        team_id_by_name = {
            normalize_nba_team_name(team["full_name"]): int(team["id"])
            for team in nba_teams.get_teams()
        }
        wanted_team_ids = {
            team_id_by_name[normalize_nba_team_name(name)]
            for player in players
            if (name := player.get("nba_team_name"))
            and normalize_nba_team_name(name) in team_id_by_name
        }
        games = [
            game
            for game in scoreboard.get("games", [])
            if int(game.get("gameStatus", 0)) in {2, 3}
            and (
                not wanted_team_ids
                or int(game.get("homeTeam", {}).get("teamId", 0)) in wanted_team_ids
                or int(game.get("awayTeam", {}).get("teamId", 0)) in wanted_team_ids
            )
        ]
        results = {}
        for game in games:
            boxscore_response = session.get(
                f"{LIVE_DATA_BASE}/boxscore/boxscore_{game['gameId']}.json",
                headers=headers,
                timeout=15,
            )
            boxscore_response.raise_for_status()
            boxscore = boxscore_response.json().get("game", {})
            game_status = "Live" if int(game.get("gameStatus", 0)) == 2 else "Final"
            for side in ("homeTeam", "awayTeam"):
                for player in boxscore.get(side, {}).get("players", []):
                    nba_player_id = int(player.get("personId", 0))
                    if nba_player_id not in nba_player_ids:
                        continue
                    points = int(player.get("statistics", {}).get("points", 0) or 0)
                    results[nba_player_id] = {
                        "game_date": game_date,
                        "points": points,
                        "status": game_status,
                    }
        return results, None
    except Exception as exc:
        return {}, str(exc)
    finally:
        session.close()


def _refresh_player_log(player: dict, season: str) -> tuple[str | None, str]:
    # Serialize history requests across viewers and stop retrying every player
    # when the shared stats endpoint is unreachable. Live requests use no lock.
    with _history_lock:
        return _refresh_player_log_locked(player, season)


def _refresh_player_log_locked(player: dict, season: str) -> tuple[str | None, str]:
    global _history_retry_at
    nba_player_id = player["nba_player_id"]
    if nba_player_id is None:
        return None, "NBA player ID unavailable"

    player_id = player["player_id"]
    fetched_at = database.get_player_log_sync_time(player_id, season)
    if fetched_at and datetime.now() - fetched_at < LOG_CACHE_TTL:
        return None, "Cached"
    if monotonic() < _history_retry_at:
        return HISTORY_WARNING, "Using cached data; retry delayed" if fetched_at else "NBA data unavailable; retry delayed"

    try:
        game_log = playergamelog.PlayerGameLog(
            player_id=str(nba_player_id),
            season=season,
            season_type_all_star="Regular Season",
            timeout=20,
        ).get_data_frames()[0]
        games = []
        for _, row in game_log.iterrows():
            game_date = datetime.strptime(str(row["GAME_DATE"]), "%b %d, %Y").date()
            games.append(
                {
                    "game_date": game_date.isoformat(),
                    "matchup": str(row["MATCHUP"]),
                    "points": int(row["PTS"]),
                }
            )
        database.save_player_season_logs(player_id, season, games)
        return None, "Updated"
    except requests.RequestException as exc:
        _history_retry_at = monotonic() + HISTORY_RETRY_SECONDS
        _logger.warning("NBA historical game-log request failed; retrying in %ss: %s", HISTORY_RETRY_SECONDS, exc)
        return HISTORY_WARNING, "Using cached data; refresh failed" if fetched_at else "NBA data unavailable"
    except Exception as exc:
        _logger.warning("NBA historical game logs could not be processed: %s", exc)
        if fetched_at:
            return HISTORY_WARNING, "Using cached data; refresh failed"
        return HISTORY_WARNING, "NBA data unavailable"


def refresh_player_logs_for_week(
    players: list[dict], week_start: date
) -> list[str]:
    """Refresh stale logs for rostered players, including bench players."""
    season = season_for_week(week_start)
    warnings = []
    for player in players:
        error, status = _refresh_player_log(player, season)
        if error and HISTORY_WARNING not in warnings:
            warnings.append(HISTORY_WARNING)
    return warnings


def refresh_roster_game_logs(players: list[dict], week_start: date) -> list[str]:
    """Populate all rostered players from the working completed-boxscore feed."""
    from free_agent_service import _fetch_stats_from_boxscores
    season = season_for_week(week_start)
    _fetch_stats_from_boxscores(season, week_start, week_start + timedelta(days=6))
    player_ids = {p["nba_player_id"]: p["player_id"] for p in players if p["nba_player_id"] is not None}
    games = []
    for game in database.get_completed_boxscores(season):
        if not week_start.isoformat() <= game["game_date"] <= (week_start + timedelta(days=6)).isoformat():
            continue
        for player in game["players"]:
            if player["nba_player_id"] in player_ids:
                games.append({"player_id": player_ids[player["nba_player_id"]], "game_date": game["game_date"],
                              "points": player["points"], "status": "Final"})
    database.save_roster_game_points(season, games)
    return []


def update_scores(
    team_id: int,
    week_start: date,
    players_by_slot: dict[str, dict] | None = None,
    *,
    refresh_logs: bool = True,
) -> tuple[list[dict], list[str]]:
    """Refresh stale game logs and save all active-player scores for a team/week."""
    if players_by_slot is None:
        players_by_slot = {
            player["slot"]: dict(player)
            for player in database.get_week_active_roster(team_id, week_start)
        }
    if not players_by_slot:
        return [], ["No active roster has been submitted for this team and week."]

    season = season_for_week(week_start)
    scores = []
    warnings = []
    _, saved_games = database.get_week_score_snapshot(team_id, week_start)

    live_points = {}
    today = date.today()
    if week_start <= today <= week_start + timedelta(days=6):
        roster = [dict(row) for row in database.get_team_roster(team_id, week_start)]
        roster_by_id = {p["player_id"]: p for p in [*roster, *players_by_slot.values()]}
        live_points, live_error = get_live_player_points(list(roster_by_id.values()))
        database.save_roster_game_points(season, [
            {"player_id": player["player_id"], "game_date": game["game_date"].isoformat(),
             "points": game["points"], "status": game["status"]}
            for player in roster_by_id.values()
            if (game := live_points.get(player["nba_player_id"]))
            and week_start <= game["game_date"] <= week_start + timedelta(days=6)
        ])
        if live_error:
            warnings.append(
                f"Live NBA scores unavailable; showing the last saved scores. {live_error}"
            )

    for slot, player in players_by_slot.items():
        if refresh_logs:
            error, status = _refresh_player_log(player, season)
        else:
            error, status = None, "Saved game logs"
        if error and HISTORY_WARNING not in warnings:
            warnings.append(HISTORY_WARNING)

        games_by_day = {
            game["game_date"]: {"game_date": game["game_date"], "points": game["points"], "status": game["status"]}
            for game in saved_games if game["player_id"] == player["player_id"]
        }
        # Keep observations from the live feed ahead of stale historical data.
        # A completed historical log can finalize a prior day's unfinished game.
        for game in database.get_player_week_games(player["player_id"], season, week_start):
            previous = games_by_day.get(game["game_date"])
            if not previous or previous["status"] == "Historical" or (
                previous["status"] == "Live" and game["game_date"] < today.isoformat()
            ):
                games_by_day[game["game_date"]] = game
        live_game = live_points.get(int(player["nba_player_id"])) if player["nba_player_id"] else None
        if live_game and week_start <= live_game["game_date"] <= week_start + timedelta(days=6):
            games_by_day[live_game["game_date"].isoformat()] = {
                "game_date": live_game["game_date"].isoformat(),
                "points": live_game["points"], "status": live_game["status"],
            }
            status = f"{status} ({live_game['status'].lower()})"
        games = [games_by_day[day] for day in sorted(games_by_day)]
        points = sum(game["points"] for game in games)
        scores.append(
            {
                "player_id": player["player_id"],
                "slot": slot,
                "points": points,
                "status": status,
                "games": games,
                "live_date": live_game["game_date"].isoformat() if live_game else None,
                "live_points": live_game["points"] if live_game else None,
                "live_status": live_game["status"] if live_game else None,
            }
        )

    database.replace_week_player_scores(team_id, week_start, scores)
    return scores, warnings


def recalculate_team_week(
    team_id: int,
    week_start: date,
    players_by_slot: dict[str, dict],
) -> tuple[list[dict], list[str]]:
    """Backward-compatible wrapper for roster submission score updates."""
    return update_scores(team_id, week_start, players_by_slot)
