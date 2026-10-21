"""Exercise Cloud DB-API adaptation with an autocommit SQLite stand-in."""

import sqlite3
import unittest
from unittest.mock import patch

import database
from cloud_database import CloudConnection, connect_cloud
from migrate_to_sqlite_cloud import migrate_backup
import test_score_consistency as fixtures


class CloudScoreConsistencyTests(fixtures.ScoreConsistencyTests):
    def setUp(self):
        super().setUp()
        self.local_path = database.DATABASE_PATH
        self.cloud_patch = patch.object(database, "CLOUD_URL", "sqlitecloud://test:8860/league.db?apikey=test")
        self.driver_patch = patch("sqlitecloud.connect", side_effect=lambda url: sqlite3.connect(self.local_path, isolation_level=None))
        self.cloud_patch.start()
        self.driver_patch.start()

    def tearDown(self):
        self.driver_patch.stop()
        self.cloud_patch.stop()
        super().tearDown()

    def test_batch_catalog_sync_preserves_lineup_ids_and_skips_unchanged_data(self):
        catalog = [{"nba_player_id": 123, "source_key": "renamed", "full_name": "Renamed Player",
                    "position": "G", "nba_team_name": "Boston Celtics", "jersey_number": "1"}]
        self.assertEqual(database.sync_player_catalog(catalog), 1)
        with database.connect() as connection:
            self.assertEqual(connection.execute("SELECT player_id FROM players").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT player_id FROM weekly_active_rosters").fetchone()["player_id"], 1)
        with patch.object(CloudConnection, "executemany", side_effect=AssertionError("Unchanged catalog rewritten")):
            self.assertEqual(database.sync_player_catalog(catalog), 1)

    def test_backup_import_into_empty_cloud_database_and_refuse_overwrite(self):
        self.refresh(18)
        remote_path = self.local_path.with_name("remote.db")
        url = "sqlitecloud://test:8860/league.db?apikey=test"
        with patch("sqlitecloud.connect", side_effect=lambda url: sqlite3.connect(remote_path, isolation_level=None)):
            self.assertGreater(migrate_backup(self.local_path, url), 0)
            self.assert_totals(48)
            with self.assertRaisesRegex(ValueError, "already has data"):
                migrate_backup(self.local_path, url)
            self.assert_totals(48)


class CloudAdapterTests(unittest.TestCase):
    def test_multistatement_schema_and_rows(self):
        with CloudConnection(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            connection.executescript("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO test(name) VALUES ('a;b');")
            row = connection.execute("SELECT * FROM test").fetchone()
            self.assertEqual(dict(row), {"id": 1, "name": "a;b"})
            self.assertEqual(row[0], 1)
            self.assertEqual(row["NAME"], "a;b")

    def test_connection_failure_does_not_fallback_or_reveal_api_key(self):
        with patch("sqlitecloud.connect", side_effect=RuntimeError("apikey=secret")):
            with self.assertRaises(RuntimeError) as error:
                connect_cloud("sqlitecloud://test:8860/league.db?apikey=secret")
            self.assertNotIn("secret", str(error.exception))

    def test_database_name_required(self):
        with self.assertRaises(ValueError):
            connect_cloud("sqlitecloud://test:8860?apikey=test")


if __name__ == "__main__":
    unittest.main()
