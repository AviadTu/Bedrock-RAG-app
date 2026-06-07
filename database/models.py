"""
database/models.py
──────────────────
SQLite persistence layer for session-based chat history.

Schema
──────
sessions
  session_id  TEXT  PRIMARY KEY
  created_at  TEXT  ISO-8601 timestamp

messages
  id              INTEGER  PRIMARY KEY AUTOINCREMENT
  session_id      TEXT     FK → sessions.session_id
  role            TEXT     'user' | 'assistant'
  content         TEXT     message text
  retrieved_json  TEXT     JSON array of source references (assistant turns only)
  created_at      TEXT     ISO-8601 timestamp

uploaded_documents
  id                INTEGER  PRIMARY KEY AUTOINCREMENT
  original_filename TEXT     filename as supplied by the uploader (verbatim,
                             may contain non-ASCII / Hebrew characters)
  s3_key            TEXT     full S3 key (system-of-record), UNIQUE
  upload_timestamp  TEXT     ISO-8601 timestamp

Design notes
  • A single SQLite file is enough for development and small deployments.
  • All writes use parameterised queries to prevent SQL injection.
  • The DB is initialised lazily via init_db(); Flask calls this at startup.
  • retrieved_json stores the Knowledge Base source references so the UI
    can display which documents were used to generate each answer.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import settings


# ─────────────────────────────────────────────────────────────────────
# Connection helper
# ─────────────────────────────────────────────────────────────────────

@contextmanager
def _get_db():
    """
    Context manager that yields a sqlite3 connection and commits on
    success or rolls back on exception.
    """
    db_path = settings.DB_PATH
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # better write concurrency
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# Schema initialisation
# ─────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables if they do not already exist. Safe to call repeatedly."""
    with _get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT    NOT NULL,
                role            TEXT    NOT NULL,
                content         TEXT    NOT NULL,
                retrieved_json  TEXT,
                created_at      TEXT    NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages (session_id, id);

            CREATE TABLE IF NOT EXISTS uploaded_documents (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filename TEXT    NOT NULL,
                s3_key            TEXT    NOT NULL UNIQUE,
                upload_timestamp  TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_uploaded_documents_key
                ON uploaded_documents (s3_key);
            """
        )


# ─────────────────────────────────────────────────────────────────────
# Write helpers
# ─────────────────────────────────────────────────────────────────────

def _ensure_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Insert a session row if it does not exist yet."""
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, created_at) VALUES (?, ?)",
        (session_id, _now()),
    )


def save_message(
    session_id: str,
    role: str,
    content: str,
    retrieved: list[dict] | None = None,
) -> None:
    """
    Persist one message to the database.

    Parameters
    ----------
    session_id : unique string identifying the browser session
    role       : 'user' or 'assistant'
    content    : text of the message
    retrieved  : list of source-reference dicts (for assistant turns)
    """
    retrieved_json = json.dumps(retrieved, ensure_ascii=False) if retrieved else None
    with _get_db() as conn:
        _ensure_session(conn, session_id)
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, retrieved_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, content, retrieved_json, _now()),
        )


# ─────────────────────────────────────────────────────────────────────
# Read helpers
# ─────────────────────────────────────────────────────────────────────

def get_history(session_id: str) -> list[dict]:
    """
    Return all messages for a session in chronological order.

    Each dict contains: id, role, content, retrieved (list|None), created_at.
    """
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, retrieved_json, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()

    history = []
    for row in rows:
        entry = dict(row)
        raw = entry.pop("retrieved_json", None)
        entry["retrieved"] = json.loads(raw) if raw else None
        history.append(entry)

    return history


def clear_session(session_id: str) -> None:
    """Delete all messages for a session (the session row is kept)."""
    with _get_db() as conn:
        conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )


# ─────────────────────────────────────────────────────────────────────
# Uploaded-document registry
# ─────────────────────────────────────────────────────────────────────

def record_upload(original_filename: str, s3_key: str) -> None:
    """
    Persist a single uploaded-document record.

    The full S3 key is the system-of-record; the original filename is stored
    verbatim (including any non-ASCII / Hebrew characters) so the UI can show
    a human-friendly label while internal code always uses the key.

    Idempotent on s3_key: re-uploading to the same key updates the stored
    original filename rather than raising on the UNIQUE constraint.
    """
    with _get_db() as conn:
        conn.execute(
            """
            INSERT INTO uploaded_documents (original_filename, s3_key, upload_timestamp)
            VALUES (?, ?, ?)
            ON CONFLICT(s3_key) DO UPDATE SET
                original_filename = excluded.original_filename,
                upload_timestamp  = excluded.upload_timestamp
            """,
            (original_filename, s3_key, _now()),
        )


def list_uploads() -> list[dict]:
    """Return all uploaded-document records, newest first."""
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, original_filename, s3_key, upload_timestamp
            FROM uploaded_documents
            ORDER BY id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def delete_upload(s3_key: str) -> bool:
    """
    Remove a document record by its S3 key.
    Returns True if a row was deleted, False if the key was not found.
    """
    with _get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM uploaded_documents WHERE s3_key = ?",
            (s3_key,),
        )
        return cursor.rowcount > 0


def get_display_name(s3_key: str) -> str | None:
    """
    Return the original filename for an S3 key, or None if the key was not
    registered via the upload endpoint (e.g. placed manually with `aws s3 cp`).
    """
    with _get_db() as conn:
        row = conn.execute(
            "SELECT original_filename FROM uploaded_documents WHERE s3_key = ?",
            (s3_key,),
        ).fetchone()
    return row["original_filename"] if row else None


# ─────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
