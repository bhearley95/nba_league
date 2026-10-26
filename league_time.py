"""Use Hawaii time for league dates and the Monday scoring-week rollover."""

from datetime import date, datetime
from zoneinfo import ZoneInfo


LEAGUE_TIMEZONE = ZoneInfo("Pacific/Honolulu")


def league_today(now: datetime | None = None) -> date:
    """Return the league date, independent of the server's timezone."""
    if now is None:
        now = datetime.now(LEAGUE_TIMEZONE)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    return now.astimezone(LEAGUE_TIMEZONE).date()
