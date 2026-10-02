"""Datalag: PostgreSQL hvis DATABASE_URL er satt, ellers lokal SQLite.

Tabeller:
  - conversations + messages : samtalehistorikk (minne)
  - memory                   : fakta/notater agenten har lært seg
  - tasks                    : jobber agenten oppretter og fullfører

SQL skrives med ?-plassholdere og oversettes til %s for PostgreSQL.
"""
import os
import sqlite3
import threading

from loguru import logger

import config

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT DEFAULT 'Ny samtale',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS memory (
        key TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        kind TEXT DEFAULT 'note',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        result TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        is_vip BOOLEAN DEFAULT FALSE,
        is_banned BOOLEAN DEFAULT FALSE,
        ban_reason TEXT,
        friend_notes TEXT,
        banned_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY,
        user_id TEXT UNIQUE NOT NULL,
        plan_name TEXT DEFAULT 'Annual Peace Pass',
        price_eur NUMERIC DEFAULT 30.00,
        status TEXT DEFAULT 'pending',
        started_at TIMESTAMP,
        expires_at TIMESTAMP,
        payment_method TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS access_logs (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        endpoint TEXT NOT NULL,
        allowed BOOLEAN DEFAULT TRUE,
        blocked_reason TEXT,
        ip_address TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS idx_subs_user_id ON subscriptions(user_id)",
]


class Database:
    def __init__(self, connection, backend: str):
        self.conn = connection
        self.backend = backend  # "postgres" eller "sqlite"
        # Én lås for all DB-tilgang. SQLite-tilkoblinger kan ikke brukes fra
        # flere tråder samtidig, og FastAPI kjører endepunktene i trådpoolen.
        self._lock = threading.Lock()

    def _sql(self, query: str) -> str:
        if self.backend == "postgres":
            return query.replace("?", "%s")
        return query

    def execute(self, query: str, params: tuple = ()) -> None:
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute(self._sql(query), params)
                self.conn.commit()
            finally:
                cur.close()

    def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute(self._sql(query), params)
                rows = cur.fetchall()
                if not rows:
                    return []
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
            finally:
                cur.close()

    def fetchone(self, query: str, params: tuple = ()) -> dict | None:
        rows = self.fetchall(query, params)
        return rows[0] if rows else None


_db: Database | None = None


def _init_sqlite() -> Database:
    data_dir = config.BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False + låsen i Database-klassen gjør det trygt at
    # FastAPI bruker tilkoblingen fra forskjellige worker-tråder.
    conn = sqlite3.connect(data_dir / "memory.db", check_same_thread=False)
    return Database(conn, "sqlite")


def _init_postgres() -> Database:
    import psycopg

    conn = psycopg.connect(DATABASE_URL)
    return Database(conn, "postgres")


def init_db() -> None:
    """Kobler til databasen og oppretter tabellene. Trygg å kalle flere ganger."""
    global _db
    if _db is not None:
        return

    if DATABASE_URL:
        try:
            _db = _init_postgres()
            logger.info("Koblet til PostgreSQL.")
        except Exception as e:
            logger.error("PostgreSQL feilet ({}). Faller tilbake til lokal SQLite.", e)
            _db = _init_sqlite()
    else:
        logger.warning(
            "DATABASE_URL mangler i .env - bruker lokal SQLite (deles ikke mellom maskiner)."
        )
        _db = _init_sqlite()

    for stmt in _SCHEMA:
        try:
            _db.execute(stmt)
        except Exception as e:
            logger.error("Kunne ikke opprette tabell: {} - {}", stmt[:60], e)


def get_db() -> Database | None:
    if _db is None:
        init_db()
    return _db
