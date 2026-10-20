"""Autentisering og brukerdatabase (lokal SQL på serveren).

Bruker innebygde Python-moduler (hashlib.pbkdf2_hmac + HMAC-SHA256 JWT + sqlite3).
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid

from loguru import logger

import database


def _db():
    return database.get_db()


# --------------------------------------------------------------------------
# Passord-hashing (PBKDF2-HMAC-SHA256 i standardbiblioteket)
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 200_000
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        if hashed.startswith("pbkdf2_sha256$"):
            _, iter_str, salt, expected_hex = hashed.split("$", 3)
            dk = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("utf-8"),
                int(iter_str),
            )
            return hmac.compare_digest(dk.hex(), expected_hex)
        if hashed.startswith("$2"):
            import bcrypt  # valgfri fallback for eldre hasher
            return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception as e:
        logger.warning("Passord-sjekk feilet: {}", e)
    return False


# --------------------------------------------------------------------------
# JWT (HS256 med innebygd hmac/hashlib - ingen ekstern pakke nødvendig)
# --------------------------------------------------------------------------

def _get_jwt_secret() -> bytes:
    secret = (
        os.environ.get("JWT_SECRET")
        or os.environ.get("API_TOKEN")
        or "servidor123-super-secret-jwt-key"
    ).strip()
    return secret.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def create_token(user: dict, expires_days: int = 365) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user.get("role") or "user",
        "iat": now,
        "exp": now + expires_days * 86400,
    }
    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(_get_jwt_secret(), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64url_encode(sig)}"


def decode_token(token: str) -> dict:
    parts = (token or "").strip().split(".")
    if len(parts) != 3:
        raise ValueError("Ugyldig JWT-format")
    h_b64, p_b64, s_b64 = parts
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    expected_sig = hmac.new(_get_jwt_secret(), signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode(s_b64)
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("Ugyldig JWT-signatur")
    payload = json.loads(_b64url_decode(p_b64).decode("utf-8"))
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and time.time() > exp:
        raise ValueError("Token er utløpt")
    return payload


# --------------------------------------------------------------------------
# Brukere i lokal SQL-database
# --------------------------------------------------------------------------

def get_user_by_username(username: str) -> dict | None:
    clean = (username or "").strip()
    if not clean:
        return None
    db = _db()
    row = db.fetchone("SELECT * FROM users WHERE username = ?", (clean,))
    if row:
        return row
    # Case-insensitivt oppslag (slik at 'Hollman Rivero' og 'Hollman Rivero' begge treffer)
    return db.fetchone("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (clean,))


def get_user_by_id(user_id: str) -> dict | None:
    if not user_id:
        return None
    db = _db()
    return db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))


def any_admin() -> bool:
    db = _db()
    return db.fetchone("SELECT id FROM users WHERE role = 'admin' LIMIT 1") is not None


def list_all_users() -> list[dict]:
    db = _db()
    rows = db.fetchall(
        "SELECT id, username, email, role, is_vip, is_banned, ban_reason, "
        "last_login_at, last_ip, login_count, created_at, updated_at "
        "FROM users ORDER BY created_at ASC"
    )
    return rows


def log_user_activity(
    endpoint: str,
    user: dict | None = None,
    ip_address: str = "",
    allowed: bool = True,
    blocked_reason: str = "",
) -> None:
    try:
        db = _db()
        db.execute(
            "INSERT INTO access_logs (id, user_id, username, endpoint, allowed, blocked_reason, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                user.get("id") if user else None,
                user.get("username") if user else None,
                endpoint,
                1 if allowed else 0,
                blocked_reason,
                ip_address,
            ),
        )
    except Exception as e:
        logger.debug("Kunne ikke logge brukeraktivitet: {}", e)


def log_error(
    error_message: str,
    source: str = "backend",
    endpoint: str = "",
    status_code: int = 500,
    details: str = "",
    user: dict | None = None,
    username: str = "",
    ip_address: str = "",
) -> dict:
    err_id = uuid.uuid4().hex
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    uid = user.get("id") if user else None
    uname = (user.get("username") if user else None) or username or None
    msg = (error_message or "Ukjent feil").strip()[:1000]
    det = (details or "").strip()[:4000]
    try:
        db = _db()
        db.execute(
            "INSERT INTO error_logs (id, user_id, username, source, endpoint, status_code, error_message, details, ip_address, resolved, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (err_id, uid, uname, source, endpoint, int(status_code or 500), msg, det, ip_address, now_str),
        )
    except Exception as e:
        logger.debug("Kunne ikke lagre feilmelding i SQL: {}", e)
    return {
        "id": err_id,
        "username": uname,
        "source": source,
        "endpoint": endpoint,
        "status_code": status_code,
        "error_message": msg,
        "details": det,
        "created_at": now_str,
    }


def list_error_logs(only_unresolved: bool = False, limit: int = 50) -> list[dict]:
    db = _db()
    if only_unresolved:
        return db.fetchall(
            "SELECT * FROM error_logs WHERE resolved = 0 ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
    return db.fetchall(
        "SELECT * FROM error_logs ORDER BY created_at DESC LIMIT ?",
        (int(limit),),
    )


def mark_errors_resolved(error_id: str = "") -> int:
    db = _db()
    if error_id and error_id != "all":
        db.execute("UPDATE error_logs SET resolved = 1 WHERE id = ?", (error_id,))
    else:
        db.execute("UPDATE error_logs SET resolved = 1 WHERE resolved = 0")
    return 1


def create_user(
    username: str,
    password: str,
    email: str | None = None,
    role: str = "user",
    is_vip: bool = True,
) -> dict:
    clean_name = username.strip()
    if not clean_name:
        raise ValueError("Brukernavn mangler")
    if get_user_by_username(clean_name):
        raise ValueError("Brukernavnet er allerede i bruk")

    # Standard rolletildeling ved opprettelse
    final_role = "user" if any_admin() else "admin"

    db = _db()
    uid = uuid.uuid4().hex
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO users (id, username, email, password_hash, role, is_vip, is_banned, login_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
        (uid, clean_name, email, hash_password(password), final_role, 1 if is_vip else 0, now_str, now_str),
    )
    # Opprett aktiv tilgangsrad i subscriptions
    try:
        db.execute(
            "INSERT INTO subscriptions (id, user_id, plan_name, price_eur, status, started_at) "
            "VALUES (?, ?, ?, 0.0, 'active', ?)",
            (uuid.uuid4().hex, uid, "Admin Pass" if final_role == "admin" else "Standard Pass", now_str),
        )
    except Exception:
        pass

    # Synkroniser ny bruker til hovedserverens SQL-database i bakgrunnen (hvis dette er en venn sin server)
    central_url = os.environ.get("CENTRAL_AUTH_URL", "http://100.115.248.101:8000").strip().rstrip("/")
    if central_url:
        import threading
        import urllib.request

        def _sync_reg():
            try:
                req = urllib.request.Request(
                    f"{central_url}/auth/register",
                    data=json.dumps({"username": clean_name, "password": password, "email": email}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=4).read()
            except Exception:
                pass

        threading.Thread(target=_sync_reg, daemon=True).start()

    return get_user_by_id(uid) or {"id": uid, "username": clean_name, "role": final_role}


RESET_CODE_MINUTES = int(os.environ.get("RESET_CODE_MINUTES", "30"))


def create_reset_code(username: str) -> tuple[str, str] | None:
    """Lager en engangskode for passordtilbakestilling.

    Koden vises ÉN gang (til administratoren, som gir den videre til brukeren)
    og lagres bare som hash. Returnerer (kode, utløper) eller None hvis
    brukeren ikke finnes."""
    user = get_user_by_username(username)
    if not user:
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=RESET_CODE_MINUTES)
    expires_str = expires.strftime("%Y-%m-%d %H:%M:%S")
    db = _db()
    # Bare den nyeste koden skal gjelde
    db.execute("UPDATE password_resets SET used = ? WHERE user_id = ? AND used = ?", (1, user["id"], 0))
    db.execute(
        "INSERT INTO password_resets (id, user_id, username, code_hash, expires_at, used) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, user["id"], user["username"], hash_password(code), expires_str, 0),
    )
    logger.warning("Passordtilbakestilling: ny engangskode for '{}' (gyldig {} min)", user["username"], RESET_CODE_MINUTES)
    return code, expires_str


def redeem_reset_code(username: str, code: str, new_password: str) -> bool:
    """Bytter passordet hvis koden er riktig, ikke utløpt og ikke brukt."""
    user = get_user_by_username(username)
    if not user:
        return False
    clean_code = (code or "").strip()
    if not clean_code or len(new_password or "") < 4:
        return False
    db = _db()
    row = db.fetchone(
        "SELECT * FROM password_resets WHERE user_id = ? AND used = ? ORDER BY created_at DESC LIMIT 1",
        (user["id"], 0),
    )
    if not row:
        return False
    try:
        expires = datetime.datetime.strptime(str(row["expires_at"])[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if expires < now:
        logger.warning("Passordtilbakestilling: koden for '{}' er utløpt", user["username"])
        return False
    if not verify_password(clean_code, row["code_hash"] or ""):
        logger.warning("Passordtilbakestilling: feil kode for '{}'", user["username"])
        return False

    db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (
            hash_password(new_password),
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            user["id"],
        ),
    )
    db.execute("UPDATE password_resets SET used = ? WHERE id = ?", (1, row["id"]))
    logger.warning("Passordtilbakestilling: passordet til '{}' er endret", user["username"])
    return True


def login(username: str, password: str, ip_address: str = "") -> dict | None:
    user = get_user_by_username(username)
    if not user:
        return None
    if not verify_password(password, user.get("password_hash") or ""):
        return None

    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    count = int(user.get("login_count") or 0) + 1
    try:
        db = _db()
        db.execute(
            "UPDATE users SET last_login_at = ?, last_ip = ?, login_count = ?, updated_at = ? WHERE id = ?",
            (now_str, ip_address, count, now_str, user["id"]),
        )
    except Exception as e:
        logger.debug("Kunne ikke oppdatere innloggingsstatistikk: {}", e)

    return get_user_by_id(user["id"]) or user


def ensure_initial_admin(
    admin_username: str | None = None,
    admin_password: str | None = None,
) -> dict | None:
    """Initialiserer standard brukeroppsett i databasen."""
    uname = (admin_username or os.environ.get("ADMIN_USERNAME") or "").strip()
    upass = (admin_password or os.environ.get("ADMIN_PASSWORD") or "").strip()
    if not uname or not upass:
        return None

    db = _db()
    existing = get_user_by_username(uname)
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if existing:
        uid = existing["id"]
        db.execute(
            "UPDATE users SET username = ?, password_hash = ?, role = 'admin', is_vip = 1, is_banned = 0, updated_at = ? WHERE id = ?",
            (uname, hash_password(upass), now_str, uid),
        )
        db.execute("UPDATE users SET role = 'user' WHERE id != ?", (uid,))
        return get_user_by_id(uid) or existing

    db.execute("UPDATE users SET role = 'user'")
    uid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO users (id, username, email, password_hash, role, is_vip, is_banned, login_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'admin', 1, 0, 0, ?, ?)",
        (uid, uname, None, hash_password(upass), now_str, now_str),
    )
    try:
        db.execute(
            "INSERT INTO subscriptions (id, user_id, plan_name, price_eur, status, started_at) "
            "VALUES (?, ?, 'Admin Pass', 0.0, 'active', ?)",
            (uuid.uuid4().hex, uid, now_str),
        )
    except Exception:
        pass
    return get_user_by_id(uid) or {"id": uid, "username": uname, "role": "admin"}


def access_state(user: dict) -> tuple[str, str]:
    """Returnerer ('ok'|'banned', begrunnelse)."""
    if bool(user.get("is_banned")):
        return "banned", (user.get("ban_reason") or "Bannet")
    return "ok", ""


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

    return {"ok": True, "status": 200, "detail": "", "user": user}
