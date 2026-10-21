"""Copy a chosen local backup into an empty SQLite Cloud league database."""

import argparse
import os
from pathlib import Path
import sqlite3
import tomllib

import database
from cloud_database import connect_cloud


def migrate_backup(source_path: Path, url: str) -> int:
    # mode=ro prevents a mistyped filename from creating a new, empty source.
    source = sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        source.execute("BEGIN")
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("The source backup failed its integrity check.")
        if source.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("The source backup contains invalid roster references.")
        tables = [row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        if not {"fantasy_teams", "players", "weekly_active_rosters"}.issubset(tables):
            raise ValueError("Select a current league database backup.")
        if "player_id" not in {row[1] for row in source.execute("PRAGMA table_info(players)")}:
            raise ValueError("Migrate the old prototype database locally first.")
        with connect_cloud(url) as target:
            database._create_tables(target)
            target.execute("PRAGMA foreign_keys = OFF")
            target.execute("BEGIN IMMEDIATE")
            target_tables = {row[0] for row in target.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if not set(tables).issubset(target_tables):
                raise ValueError("Source contains tables outside the league schema.")
            for name in target_tables:
                quoted = '"' + name.replace('"', '""') + '"'
                if target.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]:
                    raise ValueError("The cloud database already has data; nothing was imported or overwritten.")
            imported = 0
            for name in tables:
                quoted = '"' + name.replace('"', '""') + '"'
                cursor = source.execute(f"SELECT * FROM {quoted}")
                columns = ",".join('"' + item[0].replace('"', '""') + '"' for item in cursor.description)
                rows = cursor.fetchall()
                if rows:
                    placeholders = ",".join("?" for _ in rows[0])
                    target.executemany(f"INSERT INTO {quoted} ({columns}) VALUES ({placeholders})", rows)
                    imported += len(rows)
                if target.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0] != len(rows):
                    raise ValueError("Cloud row counts did not match the backup; import rolled back.")
            if target.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Imported roster references were invalid; import rolled back.")
        return imported
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="The backup containing the lineups to preserve")
    args = parser.parse_args()
    url = os.environ.get("SQLITECLOUD_URL", "")
    secrets = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if not url and secrets.exists():
        with secrets.open("rb") as file:
            url = tomllib.load(file).get("SQLITECLOUD_URL", "")
    if not url:
        parser.error("Set SQLITECLOUD_URL in .streamlit/secrets.toml or the environment first.")
    try:
        count = migrate_backup(args.source, url)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    except Exception:
        parser.exit(1, "Import failed. Check the backup, connection, and database permissions. No existing cloud league was replaced.\n")
    print(f"Imported and committed {count} rows. Configure Streamlit to use this cloud database next.")


if __name__ == "__main__":
    main()
