"""Tiny SQLite layer. Every call is short, so one shared connection + a lock is plenty."""
import json
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA foreign_keys=ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS printers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL UNIQUE,
    auth_id TEXT,
    auth_key TEXT,
    accept_jobs INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    has_thumb INTEGER NOT NULL DEFAULT 0,
    uploaded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    target_printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
    printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
    printer_name TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    position REAL NOT NULL,
    remote_uuid TEXT,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    created_at REAL NOT NULL,
    sent_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS materials (
    guid TEXT PRIMARY KEY, brand TEXT, material TEXT, color TEXT, color_code TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, time REAL NOT NULL, printer_id INTEGER, kind TEXT, message TEXT
);
"""

# Columns added after v1.0 - add them to existing databases.
MIGRATIONS = [
    ("printers", "kind", "TEXT NOT NULL DEFAULT 'ultimaker'"),
    ("printers", "model", "TEXT"),
    ("printers", "clear_pending", "TEXT"),   # JSON of the finished job waiting for "plate is clear"
    ("printers", "cleared_key", "TEXT"),     # last finished job the user confirmed as cleared
]

with _lock:
    _conn.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        cols = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    _conn.commit()

# Job states
QUEUED, SENDING, SENT, PRINTING, PAUSED = "queued", "sending", "sent", "printing", "paused"
FINISHED, ABORTED, FAILED, CANCELLED, ERROR = "finished", "aborted", "failed", "cancelled", "error"
ACTIVE_STATES = (SENDING, SENT, PRINTING, PAUSED)
DONE_STATES = (FINISHED, ABORTED, FAILED, CANCELLED, ERROR)

DEFAULT_SETTINGS = {
    "webhook_url": "",
    "strict_materials": "0",
    "hotfolder_autoqueue": "0",
}


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in q("SELECT key, value FROM settings")})
    return s


def set_setting(key, value):
    ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


def file_row(fid):
    r = one("SELECT * FROM files WHERE id=?", (fid,))
    if r:
        r["meta"] = json.loads(r["meta"] or "{}")
    return r


def log_event(printer_id, kind, message):
    ex("INSERT INTO events(time, printer_id, kind, message) VALUES(?,?,?,?)",
       (time.time(), printer_id, kind, message))
    ex("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")


def next_position():
    r = one("SELECT MAX(position) AS m FROM jobs")
    return (r["m"] or 0) + 1
