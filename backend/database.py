"""Datalag: PostgreSQL hvis DATABASE_URL er satt, ellers lokal SQLite (backend/data/memory.db).

Tabeller:
  - users                    : brukere (brukernavn, passord-hash, rolle, innloggingsinfo)
  - access_logs              : aktivitetslogg per bruker (IP, endepunkt, tidspunkt)
  - subscriptions            : tilgang/abonnement
  - conversations + messages : samtalehistorikk (minne)
  - memory                   : fakta/notater agenten har lært seg
  - tasks                    : jobber agenten oppretter og fullfører
"""
import os
import sqlite3
import threading
from pathlib import Path

from loguru import logger

BASE_DIR = Path(__file__).resolve().parent
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
        last_login_at TIMESTAMP,
        last_ip TEXT,
        login_count INTEGER DEFAULT 0,
        banned_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY,
        user_id TEXT UNIQUE NOT NULL,
        plan_name TEXT DEFAULT 'Standard Pass',
        price_eur NUMERIC DEFAULT 0.00,
        status TEXT DEFAULT 'active',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP,
        payment_method TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS access_logs (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        username TEXT,
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
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "memory.db", check_same_thread=False)
    return Database(conn, "sqlite")


def _init_postgres() -> Database:
    import psycopg

    conn = psycopg.connect(DATABASE_URL)
    return Database(conn, "postgres")


def init_db() -> None:
    """Kobler til lokal SQLite-database på serveren (backend/data/memory.db) og oppretter tabellene."""
    global _db
    if _db is not None:
        return

    _db = _init_sqlite()
    logger.info("Lokal SQL-database aktiv på serveren: {}", BASE_DIR / "data" / "memory.db")

    for stmt in _SCHEMA:
        try:
            _db.execute(stmt)
        except Exception as e:
            logger.error("Kunne ikke opprette tabell: {} - {}", stmt[:60], e)

    # Sørg for at nye kolonner også finnes i eksisterende SQLite-tabeller
    for alter_sql in (
        "ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN last_ip TEXT",
        "ALTER TABLE users ADD COLUMN login_count INTEGER DEFAULT 0",
        "ALTER TABLE access_logs ADD COLUMN username TEXT",
    ):
        try:
            _db.execute(alter_sql)
        except Exception:
            pass


def get_db() -> Database:
    if _db is None:
        init_db()
    assert _db is not None
    return _db
