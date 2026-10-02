"""Autentisering: bcrypt-passord, JWT-tokens og tilgangskontroll.

- hash_password / verify_password : bcrypt (med 72-byte-grensen håndtert)
- create_token / decode_token   : JWT (HS256)
- create_user / login / access_state : bruker- og abonnementslogikk

Faller tilbake på API_TOKEN som JWT-hemmelighet hvis JWT_SECRET ikke er satt.
"""
import datetime
import os
import secrets
import uuid

import bcrypt
import jwt
from loguru import logger

import config
import database


def _db():
    return database.get_db()


# --------------------------------------------------------------------------
# Passord (bcrypt)
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    # bcrypt har en grense på 72 byte; vi kutter av det som er over.
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception:
        return False


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------

_JWT_SECRET = (os.environ.get("JWT_SECRET") or config.API_TOKEN or "").strip()
if not _JWT_SECRET:
    _JWT_SECRET = secrets.token_hex(32)
    logger.warning(
        "JWT_SECRET mangler i .env - bruker midlertidig nøkkel (tokens ugyldige etter omstart)."
    )


def create_token(user: dict, expires_days: int = 30) -> str:
    now = datetime.datetime.utcnow()
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user.get("role") or "user",
        "iat": now,
        "exp": now + datetime.timedelta(days=expires_days),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])


# --------------------------------------------------------------------------
# Brukere
# --------------------------------------------------------------------------

def get_user_by_username(username: str) -> dict | None:
    db = _db()
    if db is None:
        return None
    return db.fetchone("SELECT * FROM users WHERE username = ?", (username,))


def get_user_by_id(user_id: str) -> dict | None:
    db = _db()
    if db is None:
        return None
    return db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))


def any_admin() -> bool:
    db = _db()
    if db is None:
        return True  # ingen DB -> la første registrerte bli admin
    return db.fetchone("SELECT id FROM users WHERE role = 'admin' LIMIT 1") is not None


def create_user(username: str, password: str, email: str | None = None,
                role: str = "user", is_vip: bool = False) -> dict:
    db = _db()
    if db is None:
        raise RuntimeError("Ingen database tilgjengelig (sett DATABASE_URL i .env).")
    uid = uuid.uuid4().hex
    try:
        db.execute(
            "INSERT INTO users (id, username, email, password_hash, role, is_vip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, username, email, hash_password(password), role, is_vip),
        )
    except Exception:
        # Unikhetsbrudd (samme brukernavn/e-post) eller annen DB-feil
        raise ValueError("Brukernavnet eller e-posten er allerede i bruk")
    return get_user_by_id(uid) or {"id": uid, "username": username, "role": role}


def login(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user:
        return None
    if not verify_password(password, user.get("password_hash") or ""):
        return None
    return user


# --------------------------------------------------------------------------
# Abonnement / tilgang
# --------------------------------------------------------------------------

def get_subscription(user_id: str) -> dict | None:
    db = _db()
    if db is None:
        return None
    return db.fetchone("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))


def _expires_valid(expires_at) -> bool:
    """Sjekker om et expires_at-tidspunkt fortsatt er i fremtiden."""
    if not expires_at:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)

    if isinstance(expires_at, str):
        try:
            dt = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return False
    else:
        dt = expires_at

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt > now


def access_state(user: dict) -> tuple[str, str]:
    """Returnerer ('ok'|'banned'|'expired', begrunnelse)."""
    if bool(user.get("is_banned")):
        return "banned", (user.get("ban_reason") or "Bannet")
    if user.get("role") == "admin" or bool(user.get("is_vip")):
        return "ok", ""

    sub = get_subscription(user.get("id"))
    if sub and sub.get("status") == "active" and _expires_valid(sub.get("expires_at")):
        return "ok", ""
    return "expired", "Abonnementet er utløpt eller mangler"


def check_token(token: str) -> dict:
    """Validerer et JWT og returnerer status for adgangskontrollen i main.py."""
    try:
        payload = decode_token(token)
    except Exception:
        return {"ok": False, "status": 401, "detail": "Ugyldig eller utløpt token"}

    user = get_user_by_id(payload.get("sub"))
    if not user:
        return {"ok": False, "status": 401, "detail": "Brukeren finnes ikke"}

    state, reason = access_state(user)
    if state == "banned":
        return {"ok": False, "status": 403, "detail": f"Bannet: {reason}"}
    if state == "expired":
        return {"ok": False, "status": 402, "detail": reason}

    return {"ok": True, "status": 200, "detail": "", "user": user}
