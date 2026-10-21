"""Adapt SQLite Cloud's DB-API driver to the app's SQLite transactions and rows."""

import sqlite3
from urllib.parse import urlsplit


class CloudRow(tuple):
    def __new__(cls, cursor, values):
        row = super().__new__(cls, values)
        row.columns = [column[0] for column in cursor.description]
        return row

    def keys(self):
        return self.columns

    def __getitem__(self, key):
        if isinstance(key, str):
            key = [name.casefold() for name in self.columns].index(key.casefold())
        return super().__getitem__(key)


class CloudConnection:
    def __init__(self, raw):
        self.raw = raw
        self.raw.row_factory = CloudRow
        self.in_transaction = False

    def execute(self, sql, parameters=()):
        command = sql.lstrip().split(None, 1)[0].upper().rstrip(";")
        if command in {"INSERT", "UPDATE", "DELETE", "REPLACE"} and not self.in_transaction:
            self.raw.execute("BEGIN")
            self.in_transaction = True
        result = self.raw.execute(sql, parameters)
        if command in {"BEGIN", "SAVEPOINT"}:
            self.in_transaction = True
        elif command in {"COMMIT", "END", "ROLLBACK"}:
            self.in_transaction = False
        return result

    def executemany(self, sql, parameters):
        rows = list(parameters)
        if not rows:
            return None
        if not self.in_transaction:
            self.execute("BEGIN")
        return self.raw.executemany(sql, rows)

    def executescript(self, script):
        # The Cloud driver doesn't implement executescript. Use SQLite's parser
        # to keep semicolons inside strings and trigger bodies intact.
        statement = ""
        for character in script:
            statement += character
            if character == ";" and sqlite3.complete_statement(statement):
                self.execute(statement)
                statement = ""
        if statement.strip():
            self.execute(statement)

    def commit(self):
        if self.in_transaction:
            self.raw.execute("COMMIT")
            self.in_transaction = False

    def rollback(self):
        if self.in_transaction:
            self.raw.execute("ROLLBACK")
            self.in_transaction = False

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()

    def backup(self, destination):
        """Copy a consistent remote snapshot into a local SQLite connection."""
        if not self.in_transaction:
            self.execute("BEGIN")
        schema = self.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        for item in schema:
            if item["type"] != "table":
                continue
            destination.execute(item["sql"])
            name = '"' + item["name"].replace('"', '""') + '"'
            rows = self.execute(f"SELECT * FROM {name}").fetchall()
            if rows:
                placeholders = ",".join("?" for _ in rows[0])
                destination.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows)
        for item in schema:
            if item["type"] != "table":
                destination.execute(item["sql"])
        destination.commit()


def connect_cloud(url):
    parsed = urlsplit(url)
    if parsed.scheme != "sqlitecloud" or not parsed.hostname or parsed.path in {"", "/"}:
        raise ValueError("SQLITECLOUD_URL must include the SQLite Cloud hostname and database name.")
    import sqlitecloud

    try:
        raw = sqlitecloud.connect(url)
    except Exception:
        # Connection strings contain an API key; don't include driver messages.
        raise RuntimeError("Could not connect to SQLite Cloud. Check SQLITECLOUD_URL and database permissions.") from None
    connection = CloudConnection(raw)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
    except Exception:
        connection.close()
        raise
    return connection
