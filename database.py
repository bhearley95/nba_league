"""SQLite storage for league teams, NBA players, rosters, and box scores."""

import os
import hashlib
import json
import re
import sqlite3
from collections import Counter
from league_time import league_today
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
import unicodedata


DATABASE_PATH = Path(
    os.environ.get("NBA_FANTASY_DB", Path(__file__).with_name("nba_fantasy.db"))
)
CLOUD_URL = os.environ.get("SQLITECLOUD_URL", "")
SCHEMA_VERSION = 3


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    if CLOUD_URL:
        from cloud_database import connect_cloud
        return connect_cloud(CLOUD_URL)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10, factory=_Connection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def export_database_backup() -> bytes:
    """Return a consistent SQLite backup, including committed live writes."""
    snapshot = sqlite3.connect(":memory:")
    try:
        with connect() as source:
            source.backup(snapshot)
        return snapshot.serialize()
    finally:
        snapshot.close()


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS nba_completed_boxscores (
            season TEXT NOT NULL,
            game_id TEXT NOT NULL,
            game_date TEXT NOT NULL,
            players_json TEXT NOT NULL,
            PRIMARY KEY (season, game_id)
        );

        CREATE TABLE IF NOT EXISTS roster_player_games (
            player_id INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
            season TEXT NOT NULL,
            game_date TEXT NOT NULL,
            points INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (player_id, season, game_date)
        );

        CREATE TABLE IF NOT EXISTS fantasy_teams (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            manager_name TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS players (
            player_id INTEGER PRIMARY KEY,
            nba_player_id INTEGER UNIQUE,
            source_key TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            position TEXT NOT NULL,
            nba_team_name TEXT NOT NULL,
            jersey_number TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS weekly_rosters (
            id INTEGER PRIMARY KEY,
            week_start TEXT NOT NULL
                CHECK (strftime('%w', week_start) = '1'),
            fantasy_team_id INTEGER NOT NULL
                REFERENCES fantasy_teams(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL
                REFERENCES players(player_id) ON DELETE RESTRICT,
            buybacks INTEGER NOT NULL DEFAULT 0 CHECK (buybacks >= 0),
            assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (week_start, fantasy_team_id, player_id),
            UNIQUE (week_start, player_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_active_rosters (
            week_start TEXT NOT NULL
                CHECK (strftime('%w', week_start) = '1'),
            fantasy_team_id INTEGER NOT NULL
                REFERENCES fantasy_teams(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL
                REFERENCES players(player_id) ON DELETE RESTRICT,
            slot TEXT NOT NULL CHECK (slot IN ('C', 'F1', 'F2', 'G1', 'G2')),
            submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (week_start, fantasy_team_id, slot),
            UNIQUE (week_start, fantasy_team_id, player_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_player_scores (
            week_start TEXT NOT NULL,
            fantasy_team_id INTEGER NOT NULL
                REFERENCES fantasy_teams(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL
                REFERENCES players(player_id) ON DELETE CASCADE,
            slot TEXT NOT NULL CHECK (slot IN ('C', 'F1', 'F2', 'G1', 'G2')),
            points INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok',
            recalculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (week_start, fantasy_team_id, player_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_score_games (
            week_start TEXT NOT NULL,
            fantasy_team_id INTEGER NOT NULL REFERENCES fantasy_teams(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
            game_date TEXT NOT NULL,
            points INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Final',
            PRIMARY KEY (week_start, fantasy_team_id, player_id, game_date)
        );

        CREATE TABLE IF NOT EXISTS player_log_sync (
            player_id INTEGER NOT NULL
                REFERENCES players(player_id) ON DELETE CASCADE,
            season TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (player_id, season)
        );

        CREATE TABLE IF NOT EXISTS draft_rosters (
            id INTEGER PRIMARY KEY,
            fantasy_team_id INTEGER NOT NULL
                REFERENCES fantasy_teams(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL
                REFERENCES players(player_id) ON DELETE RESTRICT,
            buybacks INTEGER NOT NULL DEFAULT 0 CHECK (buybacks >= 0),
            assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (fantasy_team_id, player_id),
            UNIQUE (player_id)
        );

        CREATE TABLE IF NOT EXISTS player_game_logs (
            player_id INTEGER NOT NULL
                REFERENCES players(player_id) ON DELETE CASCADE,
            game_date TEXT NOT NULL,
            season TEXT NOT NULL,
            matchup TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (player_id, game_date, season)
        );

        CREATE TABLE IF NOT EXISTS nba_team_schedule (
            season TEXT NOT NULL,
            game_date TEXT NOT NULL,
            nba_team_name TEXT NOT NULL,
            game_id TEXT NOT NULL,
            PRIMARY KEY (season, game_date, nba_team_name, game_id)
        );

        CREATE TABLE IF NOT EXISTS nba_player_season_stats (
            season TEXT NOT NULL,
            nba_player_id INTEGER NOT NULL,
            games_played INTEGER NOT NULL DEFAULT 0,
            points_per_game REAL NOT NULL DEFAULT 0,
            nba_team_name TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (season, nba_player_id)
        );

        CREATE TABLE IF NOT EXISTS nba_player_stats_sync (
            season TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS nba_player_period_stats (
            season TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            nba_player_id INTEGER NOT NULL,
            games_played INTEGER NOT NULL DEFAULT 0,
            points_per_game REAL NOT NULL DEFAULT 0,
            nba_team_name TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (season, period_start, period_end, nba_player_id)
        );

        CREATE TABLE IF NOT EXISTS nba_player_period_stats_sync (
            season TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (season, period_start, period_end)
        );

        CREATE INDEX IF NOT EXISTS idx_weekly_rosters_week_team
            ON weekly_rosters (week_start, fantasy_team_id);
        CREATE INDEX IF NOT EXISTS idx_draft_rosters_team
            ON draft_rosters (fantasy_team_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_active_rosters_week
            ON weekly_active_rosters (week_start, fantasy_team_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_player_scores_week
            ON weekly_player_scores (week_start, fantasy_team_id);
        CREATE INDEX IF NOT EXISTS idx_player_game_logs_date
            ON player_game_logs (game_date, player_id);
        CREATE INDEX IF NOT EXISTS idx_nba_team_schedule_season_date
            ON nba_team_schedule (season, game_date);
        """
    )


def initialize_database() -> None:
    """Create the schema and preserve data from the initial prototype schema."""
    if CLOUD_URL:
        with connect() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "players" in tables and "player_id" not in {
                row[1] for row in connection.execute("PRAGMA table_info(players)")
            }:
                raise RuntimeError("This cloud database uses the old prototype schema. Migrate a local backup before importing it.")
            _create_tables(connection)
        return
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Disable FK checks only during the one-time table migration from the prototype.
    raw = sqlite3.connect(DATABASE_PATH, timeout=10)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = OFF")
    try:
        tables = {
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "players" in tables:
            columns = {row[1] for row in raw.execute("PRAGMA table_info(players)")}
            if "player_id" not in columns:
                old_players = raw.execute("SELECT * FROM players").fetchall()
                old_rosters = (
                    raw.execute("SELECT * FROM weekly_rosters").fetchall()
                    if "weekly_rosters" in tables
                    else []
                )
                old_logs = (
                    raw.execute("SELECT * FROM player_game_logs").fetchall()
                    if "player_game_logs" in tables
                    else []
                )
                raw.execute("DROP TABLE IF EXISTS weekly_rosters")
                raw.execute("DROP TABLE IF EXISTS player_game_logs")
                raw.execute("DROP TABLE players")
                _create_tables(raw)

                player_ids: dict[int, int] = {}
                for player in old_players:
                    nba_id = player["nba_player_id"]
                    name = player["full_name"]
                    raw.execute(
                        """INSERT OR IGNORE INTO players
                           (nba_player_id, source_key, full_name, position, nba_team_name)
                           VALUES (?, ?, ?, ?, ?)""",
                        (nba_id, f"legacy:{nba_id}", name, "", ""),
                    )
                    inserted = raw.execute(
                        "SELECT player_id FROM players WHERE nba_player_id = ?", (nba_id,)
                    ).fetchone()
                    if inserted:
                        player_ids[nba_id] = inserted["player_id"]

                for roster in old_rosters:
                    nba_id = roster["nba_player_id"]
                    if nba_id in player_ids:
                        raw.execute(
                            """INSERT OR IGNORE INTO weekly_rosters
                               (week_start, fantasy_team_id, player_id, assigned_at)
                               VALUES (?, ?, ?, ?)""",
                            (
                                roster["week_start"],
                                roster["fantasy_team_id"],
                                player_ids[nba_id],
                                roster["assigned_at"],
                            ),
                        )

                for log in old_logs:
                    nba_id = log["nba_player_id"]
                    if nba_id in player_ids:
                        raw.execute(
                            """INSERT OR IGNORE INTO player_game_logs
                               (player_id, game_date, season, matchup, points, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                player_ids[nba_id],
                                log["game_date"],
                                log["season"],
                                log["matchup"],
                                log["points"],
                                log["updated_at"],
                            ),
                        )
        _create_tables(raw)
        raw.commit()
    finally:
        raw.close()


def read_player_positions_file(path: str | Path) -> list[dict]:
    """Parse the team's roster and position listing into the local player catalog."""
    from nba_api.stats.static import players as nba_players

    def normalize(value: str) -> str:
        ascii_name = unicodedata.normalize("NFKD", value)
        ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"\s+", " ", ascii_name).strip().casefold()

    nba_ids_by_name = {
        normalize(player["full_name"]): player["id"]
        for player in nba_players.get_players()
    }
    catalog = []
    seen_names: set[str] = set()
    team_name = ""
    player_row = re.compile(r"^\d+$")
    malformed_row_position = re.compile(r"^(.+?)\s+(G/F|F/C|G|F|C)$")

    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        columns = [column.strip() for column in line.split("\t")]
        if not line.strip() or columns[0].casefold() in {"no", "number"}:
            continue
        if len(columns) >= 3 and player_row.fullmatch(columns[0]):
            if not team_name or not columns[1] or not columns[2]:
                continue
            name, position, jersey_number = columns[1], columns[2], columns[0]
        elif len(columns) >= 3 and columns[0] in {"\u2014", "\u2013", "-"}:
            # A missing jersey number can leave the rest of the row in either
            # its normal columns or with the position appended to the name.
            if not team_name:
                continue
            if columns[2] in {"G", "F", "C", "G/F", "F/C"}:
                name, position = columns[1], columns[2]
            else:
                match = malformed_row_position.fullmatch(columns[1])
                if not match:
                    continue
                name, position = match.group(1), match.group(2)
            jersey_number = None
        else:
            # Conference and division labels precede team names in the source file.
            if columns[0].upper() not in {
                "EASTERN", "WESTERN", "ATLANTIC", "CENTRAL", "SOUTHEAST",
                "NORTHWEST", "PACIFIC", "SOUTHWEST",
            }:
                team_name = line.strip()
            continue

        if name:
            normalized_name = normalize(name)
            source_key = normalized_name
            if source_key in seen_names:
                source_key = f"{normalized_name}::{normalize(team_name)}"
            seen_names.add(source_key)
            catalog.append(
                {
                    "nba_player_id": nba_ids_by_name.get(normalized_name),
                    "source_key": source_key,
                    "full_name": name,
                    "position": position,
                    "nba_team_name": team_name,
                    "jersey_number": jersey_number,
                }
            )

    return catalog


def sync_player_catalog(players: Iterable[dict]) -> int:
    """Batch catalog updates and skip unchanged imports across deployments."""
    rows = list(players)
    # Include the matching version so existing cloud catalogs are repaired when
    # the import rules change even if the source file itself is unchanged.
    digest = hashlib.sha256(json.dumps({"matching_version": 2, "players": rows}, sort_keys=True).encode()).hexdigest()
    jersey_counts = Counter(
        (player["nba_team_name"], player["jersey_number"])
        for player in rows if player["jersey_number"]
    )
    with connect() as connection:
        saved = connection.execute(
            "SELECT value FROM app_metadata WHERE key = 'player_catalog_digest'"
        ).fetchone()
        if saved and saved[0] == digest:
            return connection.execute("SELECT COUNT(*) FROM players WHERE is_active = 1").fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        existing_rows = [dict(row) for row in connection.execute("SELECT * FROM players").fetchall()]
        next_id = max((row["player_id"] for row in existing_rows), default=0) + 1
        updates = []
        matched_ids = set()
        for player in rows:
            existing = None
            if player["nba_player_id"] is not None:
                existing = next((row for row in existing_rows if row["nba_player_id"] == player["nba_player_id"]), None)
            if existing is None:
                existing = next((row for row in existing_rows if row["source_key"] == player["source_key"]), None)
            if existing is None and player["jersey_number"] and jersey_counts[(player["nba_team_name"], player["jersey_number"])] == 1:
                candidates = [
                    row for row in existing_rows
                    if row["nba_team_name"] == player["nba_team_name"]
                    and row["jersey_number"] == player["jersey_number"]
                    and row["player_id"] not in matched_ids
                    and (row["nba_player_id"] is None or row["nba_player_id"] == player["nba_player_id"])
                ]
                if len(candidates) == 1:
                    existing = candidates[0]
            if existing is None:
                existing = {"player_id": next_id}
                next_id += 1
                existing_rows.append(existing)
            existing.update(player)
            matched_ids.add(existing["player_id"])
            updates.append((existing["player_id"], player["nba_player_id"], player["source_key"],
                            player["full_name"], player["position"], player["nba_team_name"], player["jersey_number"]))
        connection.execute("UPDATE players SET is_active = 0")
        connection.executemany(
            """INSERT INTO players
               (player_id, nba_player_id, source_key, full_name, position, nba_team_name, jersey_number, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(player_id) DO UPDATE SET nba_player_id = excluded.nba_player_id,
                 source_key = excluded.source_key, full_name = excluded.full_name,
                 position = excluded.position, nba_team_name = excluded.nba_team_name,
                 jersey_number = excluded.jersey_number, is_active = 1, updated_at = CURRENT_TIMESTAMP""",
            updates,
        )
        connection.execute(
            """INSERT INTO app_metadata (key, value) VALUES ('player_catalog_digest', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (digest,)
        )
        return connection.execute("SELECT COUNT(*) FROM players WHERE is_active = 1").fetchone()[0]


def create_team(name: str) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Enter a team member's name.")
    with connect() as connection:
        duplicate = connection.execute(
            "SELECT 1 FROM fantasy_teams WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if duplicate:
            raise ValueError(f"A team for {name} already exists.")
        cursor = connection.execute(
            "INSERT INTO fantasy_teams (name, manager_name) VALUES (?, ?)",
            (name, name),
        )
        return cursor.lastrowid


def get_teams() -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            "SELECT id, name, manager_name FROM fantasy_teams ORDER BY name COLLATE NOCASE"
        ).fetchall()


def get_active_players() -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """SELECT player_id, nba_player_id, full_name, position,
                      nba_team_name, jersey_number
               FROM players WHERE is_active = 1
               ORDER BY full_name COLLATE NOCASE"""
        ).fetchall()


def get_team_roster(team_id: int, week_start: date) -> list[sqlite3.Row]:
    """Return the roster effective on a week, carrying forward the latest snapshot."""
    with connect() as connection:
        saved_week = connection.execute(
            """SELECT MAX(week_start) FROM weekly_rosters
               WHERE fantasy_team_id = ? AND week_start <= ?""",
            (team_id, week_start.isoformat()),
        ).fetchone()[0]
        if saved_week is None:
            roster_table = "draft_rosters"
            week_filter = ""
            parameters = (team_id,)
        else:
            roster_table = "weekly_rosters"
            week_filter = "AND r.week_start = ?"
            parameters = (team_id, saved_week)
        return connection.execute(
            f"""SELECT p.player_id, p.nba_player_id, p.full_name, p.position, p.nba_team_name,
                      p.jersey_number, r.buybacks
               FROM {roster_table} AS r
               JOIN players AS p ON p.player_id = r.player_id
               WHERE r.fantasy_team_id = ? {week_filter}
               ORDER BY p.full_name COLLATE NOCASE""",
            parameters,
        ).fetchall()


def get_draft_roster(team_id: int) -> list[sqlite3.Row]:
    """Return a team's saved draft roster, independent of scoring weeks."""
    with connect() as connection:
        return connection.execute(
            """SELECT p.player_id, p.nba_player_id, p.full_name, p.position, p.nba_team_name,
                      p.jersey_number, r.buybacks
               FROM draft_rosters AS r
               JOIN players AS p ON p.player_id = r.player_id
               WHERE r.fantasy_team_id = ?
               ORDER BY p.full_name COLLATE NOCASE""",
            (team_id,),
        ).fetchall()


def get_players_rostered_by_other_teams(
    week_start: date, team_id: int
) -> set[int]:
    return {
        player["player_id"]
        for team in get_teams()
        if team["id"] != team_id
        for player in get_team_roster(team["id"], week_start)
    }


def get_players_drafted_by_other_teams(team_id: int) -> set[int]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT player_id FROM draft_rosters WHERE fantasy_team_id != ?",
            (team_id,),
        ).fetchall()
        return {row["player_id"] for row in rows}


ACTIVE_ROSTER_SLOTS = ("C", "F1", "F2", "G1", "G2")


def get_week_active_roster(team_id: int, week_start: date) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """SELECT ar.slot, p.player_id, p.nba_player_id, p.full_name,
                      p.position, p.nba_team_name
               FROM weekly_active_rosters AS ar
               JOIN players AS p ON p.player_id = ar.player_id
               WHERE ar.fantasy_team_id = ? AND ar.week_start = ?
               ORDER BY CASE ar.slot
                   WHEN 'C' THEN 1 WHEN 'F1' THEN 2 WHEN 'F2' THEN 3
                   WHEN 'G1' THEN 4 WHEN 'G2' THEN 5 END""",
            (team_id, week_start.isoformat()),
        ).fetchall()


def save_week_active_roster(
    team_id: int, week_start: date, players_by_slot: dict[str, int]
) -> None:
    """Save one team's five active players for a Monday-start scoring week."""
    if week_start.weekday() != 0:
        raise ValueError("The scoring week must start on a Monday.")
    if set(players_by_slot) != set(ACTIVE_ROSTER_SLOTS):
        raise ValueError("Select one center, two forwards, and two guards.")
    player_ids = list(players_by_slot.values())
    if any(player_id is None for player_id in player_ids) or len(set(player_ids)) != 5:
        raise ValueError("Select five different players for the active roster.")

    with connect() as connection:
        effective_roster_week = connection.execute(
            """SELECT MAX(week_start) FROM weekly_rosters
               WHERE fantasy_team_id = ? AND week_start <= ?""",
            (team_id, week_start.isoformat()),
        ).fetchone()[0]
        if effective_roster_week is None:
            roster_player_rows = connection.execute(
                "SELECT player_id FROM draft_rosters WHERE fantasy_team_id = ?",
                (team_id,),
            ).fetchall()
        else:
            roster_player_rows = connection.execute(
                """SELECT player_id FROM weekly_rosters
                   WHERE fantasy_team_id = ? AND week_start = ?""",
                (team_id, effective_roster_week),
            ).fetchall()
        available_ids = {row["player_id"] for row in roster_player_rows}
        if not set(player_ids).issubset(available_ids):
            raise ValueError("Active roster players must be on this team's roster for the selected week.")

        week = week_start.isoformat()
        connection.execute(
            "DELETE FROM weekly_active_rosters WHERE week_start = ? AND fantasy_team_id = ?",
            (week, team_id),
        )
        connection.executemany(
            """INSERT INTO weekly_active_rosters
                   (week_start, fantasy_team_id, player_id, slot)
               VALUES (?, ?, ?, ?)""",
            [
                (week, team_id, player_ids_by_slot, slot)
                for slot, player_ids_by_slot in players_by_slot.items()
            ],
        )


def get_week_player_scores(team_id: int, week_start: date) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """SELECT ar.slot, p.player_id, p.full_name, p.position,
                      p.nba_team_name, COALESCE(s.points, 0) AS points,
                      COALESCE(s.status, 'Not calculated') AS status
               FROM weekly_active_rosters AS ar
               JOIN players AS p ON p.player_id = ar.player_id
               LEFT JOIN weekly_player_scores AS s
                   ON s.week_start = ar.week_start
                   AND s.fantasy_team_id = ar.fantasy_team_id
                   AND s.player_id = ar.player_id
               WHERE ar.fantasy_team_id = ? AND ar.week_start = ?
               ORDER BY CASE ar.slot
                   WHEN 'C' THEN 1 WHEN 'F1' THEN 2 WHEN 'F2' THEN 3
                   WHEN 'G1' THEN 4 WHEN 'G2' THEN 5 END""",
            (team_id, week_start.isoformat()),
        ).fetchall()


def replace_week_player_scores(
    team_id: int,
    week_start: date,
    scores: list[dict],
) -> None:
    """Replace the saved point totals after an active roster is submitted."""
    week = week_start.isoformat()
    with connect() as connection:
        connection.execute(
            "DELETE FROM weekly_player_scores WHERE week_start = ? AND fantasy_team_id = ?",
            (week, team_id),
        )
        connection.executemany(
            """INSERT INTO weekly_player_scores
                   (week_start, fantasy_team_id, player_id, slot, points, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (week, team_id, score["player_id"], score["slot"], score["points"], score["status"])
                for score in scores
            ],
        )
        connection.execute(
            "DELETE FROM weekly_score_games WHERE week_start = ? AND fantasy_team_id = ?",
            (week, team_id),
        )
        connection.executemany(
            """INSERT INTO weekly_score_games
               (week_start, fantasy_team_id, player_id, game_date, points, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (week, team_id, score["player_id"], game["game_date"], game["points"], game.get("status", "Final"))
                for score in scores for game in score.get("games", [])
            ],
        )


def get_week_score_snapshot(team_id: int, week_start: date) -> tuple[list[dict], list[dict]]:
    """Read totals and their daily breakdown from the same committed refresh."""
    with connect() as connection:
        connection.execute("BEGIN")
        scores = connection.execute(
            """SELECT s.* FROM weekly_player_scores s
               JOIN weekly_active_rosters ar ON ar.week_start = s.week_start
                 AND ar.fantasy_team_id = s.fantasy_team_id AND ar.player_id = s.player_id
               WHERE s.fantasy_team_id = ? AND s.week_start = ?""",
            (team_id, week_start.isoformat()),
        ).fetchall()
        games = connection.execute(
            """SELECT g.*, p.full_name FROM weekly_score_games g
               JOIN players p ON p.player_id = g.player_id
               JOIN weekly_active_rosters ar ON ar.week_start = g.week_start
                 AND ar.fantasy_team_id = g.fantasy_team_id AND ar.player_id = g.player_id
               WHERE g.fantasy_team_id = ? AND g.week_start = ?""",
            (team_id, week_start.isoformat()),
        ).fetchall()
    return [dict(row) for row in scores], [dict(row) for row in games]


def get_player_log_sync_time(player_id: int, season: str) -> datetime | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT fetched_at FROM player_log_sync WHERE player_id = ? AND season = ?",
            (player_id, season),
        ).fetchone()
        return datetime.fromisoformat(row["fetched_at"]) if row else None


def save_player_season_logs(player_id: int, season: str, games: list[dict]) -> None:
    """Replace a player's cached season log and mark the season as fetched."""
    with connect() as connection:
        connection.execute(
            "DELETE FROM player_game_logs WHERE player_id = ? AND season = ?",
            (player_id, season),
        )
        connection.executemany(
            """INSERT INTO player_game_logs
                   (player_id, game_date, season, matchup, points)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (player_id, game["game_date"], season, game["matchup"], game["points"])
                for game in games
            ],
        )
        connection.execute(
            """INSERT INTO player_log_sync (player_id, season, fetched_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(player_id, season) DO UPDATE SET fetched_at = CURRENT_TIMESTAMP""",
            (player_id, season),
        )


def get_player_week_points(player_id: int, season: str, week_start: date) -> int:
    with connect() as connection:
        row = connection.execute(
            """SELECT COALESCE(SUM(points), 0) AS points FROM player_game_logs
               WHERE player_id = ? AND season = ?
                 AND game_date >= ? AND game_date <= date(?, '+6 days')""",
            (player_id, season, week_start.isoformat(), week_start.isoformat()),
        ).fetchone()
        return int(row["points"])


def get_player_week_games(player_id: int, season: str, week_start: date) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            """SELECT game_date, points, 'Historical' AS status FROM player_game_logs h
               WHERE player_id = ? AND season = ?
                 AND game_date >= ? AND game_date <= date(?, '+6 days')
                 AND NOT EXISTS (SELECT 1 FROM roster_player_games r WHERE r.player_id=h.player_id
                   AND r.season=h.season AND r.game_date=h.game_date)
               UNION ALL SELECT game_date, points, status FROM roster_player_games
               WHERE player_id = ? AND season = ? AND game_date >= ? AND game_date <= date(?, '+6 days')""",
            (player_id, season, week_start.isoformat(), week_start.isoformat()) * 2,
        ).fetchall()
    return [dict(row) for row in rows]


def get_players_week_totals(player_ids: list[int], season: str, week_start: date) -> dict[int, int]:
    """Read saved weekly totals for a whole roster in one database request."""
    if not player_ids:
        return {}
    placeholders = ",".join("?" for _ in player_ids)
    with connect() as connection:
        rows = connection.execute(
            f"""WITH games AS (
                SELECT player_id, season, game_date, points FROM player_game_logs h
                WHERE NOT EXISTS (SELECT 1 FROM roster_player_games r WHERE r.player_id=h.player_id
                  AND r.season=h.season AND r.game_date=h.game_date)
                UNION ALL SELECT player_id, season, game_date, points FROM roster_player_games)
                SELECT player_id, SUM(points) AS points FROM games
                WHERE season = ? AND game_date >= ? AND game_date <= date(?, '+6 days')
                  AND player_id IN ({placeholders}) GROUP BY player_id""",
            [season, week_start.isoformat(), week_start.isoformat(), *player_ids],
        ).fetchall()
    return {row["player_id"]: int(row["points"]) for row in rows}


def save_roster_game_points(season: str, games: list[dict]) -> None:
    with connect() as connection:
        connection.executemany(
            """INSERT INTO roster_player_games(player_id,season,game_date,points,status)
               VALUES (?,?,?,?,?) ON CONFLICT(player_id,season,game_date) DO UPDATE SET
               points=excluded.points, status=excluded.status""",
            [(g["player_id"], season, g["game_date"], g["points"], g["status"]) for g in games],
        )


def get_player_game_points_on_date(
    player_id: int, season: str, game_date: date
) -> int | None:
    with connect() as connection:
        row = connection.execute(
            """SELECT points FROM player_game_logs
               WHERE player_id = ? AND season = ? AND game_date = ?""",
            (player_id, season, game_date.isoformat()),
        ).fetchone()
        return int(row["points"]) if row else None


def get_nba_season_schedule(season: str) -> list[sqlite3.Row]:
    """Read the saved regular-season dates for NBA teams."""
    with connect() as connection:
        return connection.execute(
            """SELECT season, game_date, nba_team_name, game_id
               FROM nba_team_schedule WHERE season = ?
               ORDER BY game_date, nba_team_name""",
            (season,),
        ).fetchall()


def save_nba_season_schedule(season: str, games: list[dict]) -> None:
    """Replace the saved regular-season schedule for one NBA season."""
    with connect() as connection:
        connection.execute("DELETE FROM nba_team_schedule WHERE season = ?", (season,))
        connection.executemany(
            """INSERT INTO nba_team_schedule
                   (season, game_date, nba_team_name, game_id)
               VALUES (?, ?, ?, ?)""",
            [
                (season, game["game_date"], game["nba_team_name"], game["game_id"])
                for game in games
            ],
        )


def get_player_stats_sync_time(season: str) -> datetime | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT fetched_at FROM nba_player_stats_sync WHERE season = ?",
            (season,),
        ).fetchone()
        return datetime.fromisoformat(row["fetched_at"]) if row else None


def get_free_agent_rankings(season: str, week_start: date,
                            period_start: date | None = None,
                            period_end: date | None = None) -> tuple[list[dict], datetime | None]:
    """Read the top 30 eligible players in one query, including roster carry-forward."""
    if (period_start is None) != (period_end is None):
        raise ValueError("Both period start and end dates are required.")
    period = period_start is not None
    stats_table = "nba_player_period_stats" if period else "nba_player_season_stats"
    sync_table = "nba_player_period_stats_sync" if period else "nba_player_stats_sync"
    filters = "season = ?"
    parameters = [season]
    if period:
        filters += " AND period_start = ? AND period_end = ?"
        parameters += [period_start.isoformat(), period_end.isoformat()]
    with connect() as connection:
        connection.execute("BEGIN")
        synced = connection.execute(f"SELECT fetched_at FROM {sync_table} WHERE {filters}", parameters).fetchone()
        rows = connection.execute(
            f"""WITH latest AS (
                    SELECT fantasy_team_id, MAX(week_start) AS week_start
                    FROM weekly_rosters WHERE week_start <= ? GROUP BY fantasy_team_id
                ), rostered AS (
                    SELECT r.player_id FROM weekly_rosters r JOIN latest l
                      ON l.fantasy_team_id = r.fantasy_team_id AND l.week_start = r.week_start
                    UNION
                    SELECT d.player_id FROM draft_rosters d
                    WHERE NOT EXISTS (SELECT 1 FROM latest l WHERE l.fantasy_team_id = d.fantasy_team_id)
                )
                SELECT p.player_id, p.nba_player_id, p.full_name, p.position,
                       s.nba_team_name, s.games_played, s.points_per_game
                FROM {stats_table} s JOIN players p ON p.nba_player_id = s.nba_player_id
                WHERE s.{filters.replace(' AND ', ' AND s.')}
                  AND p.is_active = 1 AND s.games_played > 0
                  AND p.player_id NOT IN (SELECT player_id FROM rostered)
                ORDER BY s.points_per_game DESC, p.full_name COLLATE NOCASE LIMIT 30""",
            [week_start.isoformat(), *parameters],
        ).fetchall()
    return [dict(row) for row in rows], datetime.fromisoformat(synced["fetched_at"]) if synced else None


def get_completed_boxscores(season: str) -> list[dict]:
    with connect() as connection:
        rows = connection.execute("SELECT game_id, game_date, players_json FROM nba_completed_boxscores WHERE season = ?", (season,)).fetchall()
    return [{"game_id": row["game_id"], "game_date": row["game_date"], "players": json.loads(row["players_json"])} for row in rows]


def save_completed_boxscores(season: str, games: list[dict]) -> None:
    with connect() as connection:
        connection.executemany(
            """INSERT INTO nba_completed_boxscores(season, game_id, game_date, players_json)
               VALUES (?, ?, ?, ?) ON CONFLICT(season, game_id) DO UPDATE SET
               game_date = excluded.game_date, players_json = excluded.players_json""",
            [(season, game["game_id"], game["game_date"], json.dumps(game["players"])) for game in games],
        )


def get_season_player_stats(season: str) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """SELECT p.player_id, p.nba_player_id, p.full_name, p.position,
                      s.nba_team_name, s.games_played, s.points_per_game
               FROM nba_player_season_stats AS s
               JOIN players AS p ON p.nba_player_id = s.nba_player_id
               WHERE s.season = ? AND p.is_active = 1 AND s.games_played > 0
               ORDER BY s.points_per_game DESC, p.full_name COLLATE NOCASE""",
            (season,),
        ).fetchall()


def save_season_player_stats(season: str, stats: list[dict]) -> None:
    with connect() as connection:
        connection.execute(
            "DELETE FROM nba_player_season_stats WHERE season = ?", (season,)
        )
        connection.executemany(
            """INSERT INTO nba_player_season_stats
                   (season, nba_player_id, games_played, points_per_game, nba_team_name)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    season,
                    player["nba_player_id"],
                    player["games_played"],
                    player["points_per_game"],
                    player["nba_team_name"],
                )
                for player in stats
            ],
        )
        connection.execute(
            """INSERT INTO nba_player_stats_sync (season, fetched_at)
               VALUES (?, CURRENT_TIMESTAMP)
               ON CONFLICT(season) DO UPDATE SET fetched_at = CURRENT_TIMESTAMP""",
            (season,),
        )


def get_player_period_stats_sync_time(
    season: str, period_start: date, period_end: date
) -> datetime | None:
    with connect() as connection:
        row = connection.execute(
            """SELECT fetched_at FROM nba_player_period_stats_sync
               WHERE season = ? AND period_start = ? AND period_end = ?""",
            (season, period_start.isoformat(), period_end.isoformat()),
        ).fetchone()
        return datetime.fromisoformat(row["fetched_at"]) if row else None


def get_player_period_stats(
    season: str, period_start: date, period_end: date
) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """SELECT p.player_id, p.nba_player_id, p.full_name, p.position,
                      s.nba_team_name, s.games_played, s.points_per_game
               FROM nba_player_period_stats AS s
               JOIN players AS p ON p.nba_player_id = s.nba_player_id
               WHERE s.season = ? AND s.period_start = ? AND s.period_end = ?
                 AND p.is_active = 1 AND s.games_played > 0
               ORDER BY s.points_per_game DESC, p.full_name COLLATE NOCASE""",
            (season, period_start.isoformat(), period_end.isoformat()),
        ).fetchall()


def save_player_period_stats(
    season: str, period_start: date, period_end: date, stats: list[dict]
) -> None:
    period_start_text = period_start.isoformat()
    period_end_text = period_end.isoformat()
    with connect() as connection:
        connection.execute(
            """DELETE FROM nba_player_period_stats
               WHERE season = ? AND period_start = ? AND period_end = ?""",
            (season, period_start_text, period_end_text),
        )
        connection.executemany(
            """INSERT INTO nba_player_period_stats
                   (season, period_start, period_end, nba_player_id, games_played,
                    points_per_game, nba_team_name)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    season,
                    period_start_text,
                    period_end_text,
                    player["nba_player_id"],
                    player["games_played"],
                    player["points_per_game"],
                    player["nba_team_name"],
                )
                for player in stats
            ],
        )
        connection.execute(
            """INSERT INTO nba_player_period_stats_sync
                   (season, period_start, period_end, fetched_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(season, period_start, period_end)
               DO UPDATE SET fetched_at = CURRENT_TIMESTAMP""",
            (season, period_start_text, period_end_text),
        )


def get_week_player_game_logs(team_id: int, week_start: date) -> list[sqlite3.Row]:
    """Return daily game points for a team's active players in a scoring week."""
    with connect() as connection:
        return connection.execute(
            """SELECT p.player_id, p.full_name, g.game_date, g.points, g.matchup
               FROM weekly_active_rosters AS ar
               JOIN players AS p ON p.player_id = ar.player_id
               JOIN player_game_logs AS g ON g.player_id = p.player_id
               WHERE ar.fantasy_team_id = ? AND ar.week_start = ?
                 AND g.game_date >= ? AND g.game_date <= date(?, '+6 days')
               ORDER BY g.game_date, p.full_name COLLATE NOCASE""",
            (team_id, week_start.isoformat(), week_start.isoformat(), week_start.isoformat()),
        ).fetchall()


def save_draft_roster(team_id: int, player_ids: list[int]) -> None:
    """Replace a team's ten-player draft roster; new players start with 0 buybacks."""
    if len(player_ids) != 10 or len(set(player_ids)) != 10:
        raise ValueError("A draft roster must contain exactly ten different players.")

    with connect() as connection:
        other_team_players = connection.execute(
            "SELECT player_id FROM draft_rosters WHERE fantasy_team_id != ?",
            (team_id,),
        ).fetchall()
        unavailable = {row["player_id"] for row in other_team_players}
        if unavailable.intersection(player_ids):
            raise ValueError("One or more selected players are already on another team's draft roster.")

        for player_id in player_ids:
            previous_buybacks = connection.execute(
                """SELECT buybacks FROM draft_rosters
                   WHERE fantasy_team_id = ? AND player_id = ?""",
                (team_id, player_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO draft_rosters (fantasy_team_id, player_id, buybacks)
                   VALUES (?, ?, ?)
                   ON CONFLICT(fantasy_team_id, player_id) DO UPDATE SET
                       buybacks = MAX(draft_rosters.buybacks, excluded.buybacks)""",
                (team_id, player_id, previous_buybacks["buybacks"] if previous_buybacks else 0),
            )

        placeholders = ",".join("?" for _ in player_ids)
        connection.execute(
            f"""DELETE FROM draft_rosters
                WHERE fantasy_team_id = ? AND player_id NOT IN ({placeholders})""",
            (team_id, *player_ids),
        )


def save_team_roster(team_id: int, week_start: date, player_ids: list[int]) -> None:
    """Save a full roster snapshot effective this week without changing past weeks."""
    if week_start.weekday() != 0:
        raise ValueError("Roster week must start on a Monday.")
    if len(player_ids) != 10 or len(set(player_ids)) != 10:
        raise ValueError("A roster must contain exactly ten different players.")

    with connect() as connection:
        other_team_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM fantasy_teams WHERE id != ?", (team_id,)
            ).fetchall()
        ]
        unavailable = set()
        week = week_start.isoformat()
        for other_team_id in other_team_ids:
            prior_week = connection.execute(
                """SELECT MAX(week_start) FROM weekly_rosters
                   WHERE fantasy_team_id = ? AND week_start <= ?""",
                (other_team_id, week),
            ).fetchone()[0]
            if prior_week is None:
                roster_table = "draft_rosters"
                roster_rows = connection.execute(
                    "SELECT player_id FROM draft_rosters WHERE fantasy_team_id = ?",
                    (other_team_id,),
                ).fetchall()
            else:
                roster_table = "weekly_rosters"
                roster_rows = connection.execute(
                    """SELECT player_id FROM weekly_rosters
                       WHERE fantasy_team_id = ? AND week_start = ?""",
                    (other_team_id, prior_week),
                ).fetchall()
            unavailable.update(row["player_id"] for row in roster_rows)
        if unavailable.intersection(player_ids):
            raise ValueError("One or more selected players are already on another team.")

        for player_id in player_ids:
            previous = connection.execute(
                """SELECT MAX(buybacks) FROM weekly_rosters
                   WHERE fantasy_team_id = ? AND player_id = ?""",
                (team_id, player_id),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO weekly_rosters
                       (week_start, fantasy_team_id, player_id, buybacks)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(week_start, fantasy_team_id, player_id)
                   DO UPDATE SET buybacks = MAX(weekly_rosters.buybacks, excluded.buybacks)""",
                (week_start.isoformat(), team_id, player_id, previous or 0),
            )

        placeholders = ",".join("?" for _ in player_ids)
        connection.execute(
            f"""DELETE FROM weekly_rosters
                WHERE week_start = ? AND fantasy_team_id = ?
                  AND player_id NOT IN ({placeholders})""",
            (week_start.isoformat(), team_id, *player_ids),
        )


def get_leaderboard(week_start: date, as_of: date | None = None) -> list[dict]:
    """Return season totals, current-week points, and wins from completed weeks."""
    as_of = as_of or league_today()
    with connect() as connection:
        rows = connection.execute(
            """SELECT t.id, t.name, t.manager_name,
                      COALESCE(SUM(s.points), 0) AS total_points,
                      COALESCE(SUM(CASE WHEN s.week_start = ? THEN s.points ELSE 0 END), 0)
                          AS current_week_points
               FROM fantasy_teams AS t
               LEFT JOIN weekly_player_scores AS s
                   ON s.fantasy_team_id = t.id
               GROUP BY t.id, t.name, t.manager_name
               ORDER BY total_points DESC, t.name COLLATE NOCASE""",
            (week_start.isoformat(),),
        ).fetchall()
        score_rows = connection.execute(
            """SELECT week_start, fantasy_team_id, SUM(points) AS points,
                      COUNT(*) AS player_count
               FROM weekly_player_scores
               GROUP BY week_start, fantasy_team_id"""
        ).fetchall()
        team_count = connection.execute(
            "SELECT COUNT(*) FROM fantasy_teams"
        ).fetchone()[0]

    completed_scores: dict[str, dict[int, tuple[int, int]]] = {}
    for row in score_rows:
        week_start_day = date.fromisoformat(row["week_start"])
        if week_start_day + timedelta(days=6) >= as_of:
            continue
        completed_scores.setdefault(row["week_start"], {})[row["fantasy_team_id"]] = (
            int(row["points"]), int(row["player_count"])
        )

    weekly_wins = {row["id"]: 0 for row in rows}
    for teams_in_week in completed_scores.values():
        # Only finalize a week after every league team has a complete five-player score.
        if len(teams_in_week) != team_count or any(
            player_count < len(ACTIVE_ROSTER_SLOTS)
            for _, player_count in teams_in_week.values()
        ):
            continue
        highest_score = max(points for points, _ in teams_in_week.values())
        if highest_score <= 0:
            continue
        for team_id, (points, _) in teams_in_week.items():
            if points == highest_score:
                weekly_wins[team_id] += 1

    leaderboard = []
    for row in rows:
        team = dict(row)
        team["weekly_wins"] = weekly_wins[team["id"]]
        leaderboard.append(team)
    return leaderboard

