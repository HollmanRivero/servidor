"""Minne: samtalehistorikk, fakta/notater og jobber (tasks)."""
import uuid

import database


def _db():
    return database.get_db()


# --------------------------------------------------------------------------
# Samtaler
# --------------------------------------------------------------------------

def ensure_conversation(conversation_id: str | None, title: str = "Ny samtale") -> str:
    db = _db()
    if db is None:
        return conversation_id or "local"
    if conversation_id:
        exists = db.fetchone("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if not exists:
            db.execute("INSERT INTO conversations (id, title) VALUES (?, ?)", (conversation_id, title))
        return conversation_id
    cid = uuid.uuid4().hex
    db.execute("INSERT INTO conversations (id, title) VALUES (?, ?)", (cid, title))
    return cid


def append_message(conversation_id: str, role: str, content: str) -> None:
    db = _db()
    if db is None:
        return
    mid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO messages (id, conversation_id, role, content) VALUES (?, ?, ?, ?)",
        (mid, conversation_id, role, content),
    )
    db.execute(
        "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (conversation_id,),
    )


def history(conversation_id: str, limit: int = 20) -> list[dict]:
    """Returnerer de siste `limit` meldingene, eldst først."""
    db = _db()
    if db is None:
        return []
    return db.fetchall(
        """SELECT role, content FROM (
               SELECT role, content, created_at FROM messages
               WHERE conversation_id = ?
               ORDER BY created_at DESC LIMIT ?
           ) sub ORDER BY created_at ASC""",
        (conversation_id, limit),
    )


# --------------------------------------------------------------------------
# Fakta / notater ("bli lærd")
# --------------------------------------------------------------------------

def remember(key: str, content: str, kind: str = "note") -> None:
    db = _db()
    if db is None:
        return
    db.execute(
        """INSERT INTO memory (key, content, kind, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT (key) DO UPDATE SET
               content = excluded.content,
               kind = excluded.kind,
               updated_at = CURRENT_TIMESTAMP""",
        (key.strip(), content, kind),
    )


def recall(query: str | None = None, limit: int = 10) -> list[dict]:
    db = _db()
    if db is None:
        return []
    if query:
        like = f"%{query}%"
        return db.fetchall(
            "SELECT key, content, kind, updated_at FROM memory "
            "WHERE key LIKE ? OR content LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (like, like, limit),
        )
    return db.fetchall(
        "SELECT key, content, kind, updated_at FROM memory ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )


# --------------------------------------------------------------------------
# Jobber ("fullføre en jobb")
# --------------------------------------------------------------------------

def create_task(title: str, description: str = "") -> str:
    db = _db()
    tid = uuid.uuid4().hex
    if db is not None:
        db.execute(
            "INSERT INTO tasks (id, title, description, status) VALUES (?, ?, ?, 'pending')",
            (tid, title, description),
        )
    return tid


def update_task(task_id: str, status: str, result: str = "") -> None:
    db = _db()
    if db is None:
        return
    if status in ("done", "failed"):
        db.execute(
            "UPDATE tasks SET status = ?, result = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, result, task_id),
        )
    else:
        db.execute(
            "UPDATE tasks SET status = ?, result = ? WHERE id = ?",
            (status, result, task_id),
        )


def list_tasks(limit: int = 20) -> list[dict]:
    db = _db()
    if db is None:
        return []
    return db.fetchall(
        "SELECT id, title, description, status, result, created_at, completed_at "
        "FROM tasks ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
