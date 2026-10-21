"""Read the bundled NBA schedule without database or network dependencies."""

from datetime import date, timedelta
from functools import lru_cache
import json
from pathlib import Path
import re


SCHEDULE_DIRECTORY = Path(__file__).with_name("data")


@lru_cache(maxsize=8)
def _read_schedule(season: str) -> tuple:
    if not re.fullmatch(r"\d{4}-\d{2}", season):
        raise ValueError("Invalid NBA season.")
    path = SCHEDULE_DIRECTORY / f"schedule_{season}.json"
    if not path.exists():
        raise ValueError(f"The saved NBA schedule for {season} is missing from the deployment.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["season"] != season:
        raise ValueError("The saved schedule's season does not match its filename.")
    return tuple(
        (date.fromisoformat(game["game_date"]), game["nba_team_name"], game["game_id"])
        for game in payload["games"]
    )


def get_season_schedule(season: str, refresh: bool = False) -> list[dict]:
    if refresh:
        _read_schedule.cache_clear()
    return [
        {"game_date": day.isoformat(), "nba_team_name": team, "game_id": game_id}
        for day, team, game_id in _read_schedule(season)
    ]


def get_week_schedule(week_start: date) -> set[tuple[date, str]]:
    year = week_start.year if week_start.month >= 10 else week_start.year - 1
    season = f"{year}-{(year + 1) % 100:02d}"
    return {(day, team) for day, team, _ in _read_schedule(season)
            if week_start <= day <= week_start + timedelta(days=6)}
