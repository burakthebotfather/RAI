import sqlite3
import threading
from datetime import datetime

DB_PATH = "ratings.db"
_local = threading.local()


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ratings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            score       INTEGER NOT NULL,
            rated_at    TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plus_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            posted_at   TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_votes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            chat_id         INTEGER NOT NULL,
            admin_id        INTEGER NOT NULL,
            message_id      INTEGER NOT NULL,
            display_name    TEXT    NOT NULL DEFAULT '',
            voted           INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scheduled_deletes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            delete_at   TEXT    NOT NULL
        );
    """)
    # Миграция: добавить колонку display_name если её ещё нет (для уже существующих баз)
    try:
        conn.execute("ALTER TABLE pending_votes ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def get_rating(user_id: int) -> float:
    row = get_conn().execute(
        "SELECT SUM(score) as total, COUNT(*) as cnt FROM ratings WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    if not row or row["cnt"] == 0:
        return 5.0
    return round(row["total"] / row["cnt"], 2)


def add_rating(user_id: int, score: int) -> float:
    conn = get_conn()
    conn.execute(
        "INSERT INTO ratings (user_id, score, rated_at) VALUES (?, ?, ?)",
        (user_id, score, datetime.utcnow().isoformat())
    )
    conn.commit()
    return get_rating(user_id)


def get_last_plus(user_id: int, chat_id: int) -> datetime | None:
    row = get_conn().execute(
        "SELECT posted_at FROM plus_log WHERE user_id = ? AND chat_id = ? ORDER BY posted_at DESC LIMIT 1",
        (user_id, chat_id)
    ).fetchone()
    if row:
        return datetime.fromisoformat(row["posted_at"])
    return None


def log_plus(user_id: int, chat_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO plus_log (user_id, chat_id, posted_at) VALUES (?, ?, ?)",
        (user_id, chat_id, datetime.utcnow().isoformat())
    )
    conn.commit()


def save_pending_vote(user_id: int, chat_id: int, admin_id: int, message_id: int, display_name: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO pending_votes (user_id, chat_id, admin_id, message_id, display_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, chat_id, admin_id, message_id, display_name, datetime.utcnow().isoformat())
    )
    conn.commit()
    return cur.lastrowid


def mark_voted(vote_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE pending_votes SET voted = 1 WHERE id = ?", (vote_id,))
    conn.commit()


def get_pending_vote(vote_id: int) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM pending_votes WHERE id = ?", (vote_id,)
    ).fetchone()


def schedule_delete(chat_id: int, message_id: int, delete_at: datetime) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO scheduled_deletes (chat_id, message_id, delete_at) VALUES (?, ?, ?)",
        (chat_id, message_id, delete_at.isoformat())
    )
    conn.commit()


def get_due_deletes(now: datetime) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM scheduled_deletes WHERE delete_at <= ?",
        (now.isoformat(),)
    ).fetchall()


def remove_scheduled_delete(row_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM scheduled_deletes WHERE id = ?", (row_id,))
    conn.commit()
