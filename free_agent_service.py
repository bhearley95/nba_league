from datetime import date, datetime, timedelta, timezone

from nba_api.stats.endpoints import leaguedashplayerstats
from nba_api.stats.static import teams

import database


STATS_CACHE_TTL = timedelta(hours=3)


def _fetch_stats(
    season: str, period_start: date | None = None, period_end: date | None = None
) -> list[dict]:
    parameters = {
        "season": season,
        "season_type_all_star": "Regular Season",
        "per_mode_detailed": "PerGame",
        "measure_type_detailed_defense": "Base",
        "timeout": 30,
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
