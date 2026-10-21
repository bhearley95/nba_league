import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database


class CatalogImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(database, "DATABASE_PATH", Path(self.directory.name) / "league.db")
        self.cloud_patch = patch.object(database, "CLOUD_URL", "")
        self.path_patch.start()
        self.cloud_patch.start()
        database.initialize_database()
        self.players = [
            {"nba_player_id": 1626181, "source_key": "norman powell", "full_name": "Norman Powell", "position": "G/F", "nba_team_name": "Chicago Bulls", "jersey_number": "24"},
            {"nba_player_id": 1641815, "source_key": "isaiah stevens", "full_name": "Isaiah Stevens", "position": "G", "nba_team_name": "Chicago Bulls", "jersey_number": "24"},
        ]

    def tearDown(self):
        self.cloud_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    def test_fresh_real_catalog_keeps_norman_and_isaiah_separate(self):
        database.sync_player_catalog(database.read_player_positions_file("player_positions.txt"))
        players = {row["full_name"]: row for row in database.get_active_players()}
        self.assertEqual(players["Norman Powell"]["nba_player_id"], 1626181)
        self.assertEqual(players["Isaiah Stevens"]["nba_player_id"], 1641815)
        self.assertNotEqual(players["Norman Powell"]["player_id"], players["Isaiah Stevens"]["player_id"])

    def test_repairs_old_digest_without_changing_existing_roster_ids(self):
        # Simulate the old importer having overwritten Norman with Isaiah.
        with database.connect() as connection:
            connection.execute("INSERT INTO fantasy_teams(id, name) VALUES (1, 'Test Team')")
            connection.execute("""INSERT INTO players
                (player_id, nba_player_id, source_key, full_name, position, nba_team_name, jersey_number)
                VALUES (104, 1641815, 'isaiah stevens', 'Isaiah Stevens', 'G', 'Chicago Bulls', '24')""")
            connection.execute("INSERT INTO draft_rosters(fantasy_team_id, player_id) VALUES (1, 104)")
            old_digest = hashlib.sha256(json.dumps(self.players, sort_keys=True).encode()).hexdigest()
            connection.execute("INSERT INTO app_metadata(key,value) VALUES ('player_catalog_digest', ?)", (old_digest,))
        self.assertEqual(database.sync_player_catalog(self.players), 2)
        players = {row["full_name"]: row for row in database.get_active_players()}
        self.assertEqual(players["Isaiah Stevens"]["player_id"], 104)
        self.assertNotEqual(players["Norman Powell"]["player_id"], 104)
        self.assertEqual(database.get_draft_roster(1)[0]["full_name"], "Isaiah Stevens")
        self.assertEqual(database.sync_player_catalog(self.players), 2)

    def test_same_jersey_cannot_replace_a_different_known_nba_id(self):
        database.sync_player_catalog(self.players[1:])
        with database.connect() as connection:
            original_id = connection.execute("SELECT player_id FROM players").fetchone()[0]
        database.sync_player_catalog(self.players[:1])
        with database.connect() as connection:
            rows = connection.execute("SELECT * FROM players ORDER BY player_id").fetchall()
        self.assertEqual(rows[0]["player_id"], original_id)
        self.assertEqual(rows[0]["nba_player_id"], 1641815)
        self.assertEqual(rows[0]["is_active"], 0)
        self.assertEqual(rows[1]["nba_player_id"], 1626181)


if __name__ == "__main__":
    unittest.main()
