"""Refresh the SQLite player catalog from player_positions.txt.

Run from the project folder with:
    .\\.env\\Scripts\\python.exe update_player_database.py
"""

from pathlib import Path

import database


def main() -> None:
    player_file = Path(__file__).with_name("player_positions.txt")
    if not player_file.exists():
        raise FileNotFoundError(f"Player source file not found: {player_file}")

    database.initialize_database()
    players = database.read_player_positions_file(player_file)
    active_count = database.sync_player_catalog(players)

    print(f"Read {len(players)} players from {player_file.name}.")
    print(f"Updated {database.DATABASE_PATH} with {active_count} active players.")


if __name__ == "__main__":
    main()
