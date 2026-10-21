The league database must live in SQLite Cloud to survive Streamlit Community Cloud redeploys. GitHub sign-in to SQLite Cloud does not connect the app to the database.

1. Choose the database containing the teams and lineups you want to preserve. A backup downloaded from the running app is preferable if the server has newer data than your computer. The repository database may contain older lineups. If the backup controls aren't deployed yet, record the current lineups before redeploying; a local copy can't recover server-only edits.
2. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`. Replace the placeholders with the connection string from the SQLite Cloud Connect panel. Include the actual database name in the URL, for example `sqlitecloud://HOST:8860/league.db?apikey=KEY&timeout=10`. The secrets file is ignored by Git.
3. Install the updated requirements in your Python environment: `python -m pip install -r requirements.txt`.
4. Before pointing the running app at the cloud database, import your chosen backup into the empty cloud database:

   ```powershell
   python migrate_to_sqlite_cloud.py --source "C:\path\to\nba_league_backup.db"
   ```

   Use the real backup path. The importer copies teams, rosters, lineups, cached game data, and scores in one transaction, validates row counts and references, and refuses to overwrite any populated cloud database. It never seeds from the repository automatically. If you already uploaded the desired database through SQLite Cloud, skip the import command.
5. Commit and push the code changes. In the deployed Streamlit app's Settings > Secrets, add the same top-level secret:

   ```toml
   SQLITECLOUD_URL = "sqlitecloud://HOST:8860/league.db?apikey=KEY&timeout=10"
   ```

   Add this to any existing secrets. Do not commit the actual connection string or paste the API key into chat. Restart the app after configuring the secret.
6. Unlock admin access on Overview. Confirm it says `League storage: SQLite Cloud` and verify the imported lineups. Submit a lineup, restart the app, and confirm it remains saved.

With no cloud secret configured, the app continues to use local SQLite for development. With a cloud secret configured, connection failures stop the app instead of silently switching to a disposable local database. Local development can use a different cloud database, or omit the secret to avoid editing the production league.

Keep independent database backups. Only after confirming the cloud migration and retaining a local backup, remove `nba_fantasy.db` from Git tracking; `.gitignore` alone does not untrack an existing file. Removing the tracked database before migration can remove the only deployed copy on the next update.
