"""SQLite storage. The importer opens it read-write; the MCP server opens it read-only."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def _default_db() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "mychart-mcp" / "health.db"


DEFAULT_DB = _default_db()

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    rowid         INTEGER PRIMARY KEY,
    origin        TEXT NOT NULL,   -- how we got it: 'apple_health', 'fhir', 'ccda'
    source        TEXT,            -- health system / organization name
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    date          TEXT,            -- primary clinical date (onset, effective, performed...)
    recorded      TEXT,            -- when it was entered into the chart
    end_date      TEXT,
    status        TEXT,
    display       TEXT,
    codes         TEXT,            -- space-separated 'SYSTEM:code' tokens
    value         REAL,
    unit          TEXT,
    value_text    TEXT,
    raw           TEXT NOT NULL,
    UNIQUE (origin, source, resource_type, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_resources_type_date ON resources (resource_type, date);

CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5 (
    display, codes, body, content='', contentless_delete=1
);

CREATE TABLE IF NOT EXISTS import_meta (
    origin      TEXT PRIMARY KEY,
    imported_at TEXT NOT NULL,
    source_file TEXT,
    counts      TEXT
);
"""


def db_path() -> Path:
    return Path(os.environ.get("MYCHART_MCP_DB", DEFAULT_DB)).expanduser()


def connect_rw(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    is_new = not path.exists()
    conn = sqlite3.connect(path)
    if is_new:
        os.chmod(path, 0o600)
    conn.executescript(SCHEMA)
    return conn


def connect_ro(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    if not path.exists():
        raise FileNotFoundError(f"No database at {path}. Run `mychart-mcp import` first.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn
