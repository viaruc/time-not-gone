"""SQLite store for activity events.

Claude Code prunes old transcripts (~30 days by default), so every event we see
is copied here and kept forever. Reports are computed from this table only.
"""

import sqlite3
from pathlib import Path

DATA_DIR = Path.home() / "Library" / "Application Support" / "time-not-gone"
DB_PATH = DATA_DIR / "tng.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path   TEXT PRIMARY KEY,
    size   INTEGER NOT NULL,
    offset INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    session   TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    workspace TEXT    NOT NULL,
    source    TEXT    NOT NULL,
    PRIMARY KEY (session, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS sessions (
    session   TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    source    TEXT NOT NULL,
    title     TEXT
);
"""

# Columns added after the first release, with the statements that backfill them.
MIGRATIONS = {
    # titles you set yourself; re-read every log once to pick up old renames
    ("sessions", "custom_title"): ["ALTER TABLE sessions ADD COLUMN custom_title TEXT", "DELETE FROM files"],
}


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for (table, column), statements in MIGRATIONS.items():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
    return conn


def file_offset(conn: sqlite3.Connection, path: str) -> tuple[int, int]:
    row = conn.execute("SELECT size, offset FROM files WHERE path = ?", (path,)).fetchone()
    return (row[0], row[1]) if row else (0, 0)


def set_file_offset(conn: sqlite3.Connection, path: str, size: int, offset: int) -> None:
    conn.execute(
        "INSERT INTO files (path, size, offset) VALUES (?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET size = excluded.size, offset = excluded.offset",
        (path, size, offset),
    )


def add_events(conn: sqlite3.Connection, rows: list[tuple[str, int, str, str]]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO events (session, ts, workspace, source) VALUES (?, ?, ?, ?)", rows
    )


def upsert_session(
    conn: sqlite3.Connection, session: str, workspace: str, source: str, title: str | None
) -> None:
    conn.execute(
        "INSERT INTO sessions (session, workspace, source, title) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(session) DO UPDATE SET workspace = excluded.workspace, "
        "source = excluded.source, title = COALESCE(excluded.title, sessions.title)",
        (session, workspace, source, title),
    )


def set_title(conn: sqlite3.Connection, session: str, title: str, custom: bool) -> None:
    column = "custom_title" if custom else "title"
    conn.execute(f"UPDATE sessions SET {column} = ? WHERE session = ?", (title, session))


def session_titles(conn: sqlite3.Connection, sessions: list[str]) -> dict[str, str]:
    """Best title per session: your own name for it first, then Claude's."""
    marks = ",".join("?" * len(sessions))
    return {
        row[0]: row[1]
        for row in conn.execute(
            f"SELECT session, COALESCE(NULLIF(custom_title, ''), title) FROM sessions "
            f"WHERE session IN ({marks})",
            sessions,
        )
        if row[1]
    }


def session_info(conn: sqlite3.Connection, session: str) -> tuple[str, str] | None:
    """(workspace, source) of a session that has been seen before."""
    row = conn.execute(
        "SELECT workspace, source FROM sessions WHERE session = ?", (session,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def events_between(conn: sqlite3.Connection, start: int, end: int) -> list[tuple[str, int, str, str]]:
    return conn.execute(
        "SELECT session, ts, workspace, source FROM events WHERE ts >= ? AND ts < ? ORDER BY ts",
        (start, end),
    ).fetchall()
