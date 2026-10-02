"""Sjekker databasekoblingen til Super-Server.

Kjor fra backend-mappen:

    python check_db.py

Skriptet viser hvilken database som brukes (Neon/PostgreSQL eller lokal SQLite),
oppretter tabellene hvis de mangler, og lister dem opp. Det skriver ALDRI ut
passordet eller hele DATABASE_URL - kun vertsnavnet.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def _host_only(url: str) -> str:
    """Plukker ut vertsnavnet fra connection string - aldri bruker/passord."""
    try:
        return urlparse(url).hostname or "(ukjent vert)"
    except Exception:
        return "(ukjent vert)"


def main() -> int:
    if DATABASE_URL:
        print(f"1. DATABASE_URL er satt  ->  {_host_only(DATABASE_URL)}")
    else:
        print("1. DATABASE_URL mangler  ->  bruker lokal SQLite (backend/data/memory.db)")

    # 2. Rå tilkoblingstest, sa vi ser den ekte feilen for vi faller tilbake til SQLite.
    if DATABASE_URL:
        try:
            import psycopg

            with psycopg.connect(DATABASE_URL, connect_timeout=15) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version()")
                    version = (cur.fetchone() or [""])[0]
            print(f"2. Tilkobling OK        ->  {version.split(',')[0]}")
        except Exception as e:
            print(f"2. TILKOBLING FEILET    ->  {type(e).__name__}: {e}")
            print("   Sjekk at connection string er limt inn komplett, og at IP-en din")
            print("   er tillatt i Neon (Neon -> Settings -> Network / IP Allow).")
            return 1
    else:
        print("2. Tilkobling OK        ->  hopper over (ingen DATABASE_URL)")

    # 3. Bruk prosjektets egen datalag-kode (oppretter tabellene ved behov).
    import database  # importeres etter load_dotenv, slik config.py faar lest .env

    db = database.get_db()
    if db is None:
        print("3. FEIL                 ->  datalaget fikk ingen tilkobling")
        return 1
    print(f"3. Datalag              ->  backend = {db.backend}")

    if db.backend == "postgres":
        tables = db.fetchall(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        names = [t.get("table_name") for t in tables]
    else:
        rows = db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        names = [r.get("name") for r in rows]

    print(f"4. Tabeller ({len(names)})       ->  {', '.join(names) if names else 'INGEN'}")

    for expected in ("users", "subscriptions", "access_logs", "conversations", "messages", "memory", "tasks"):
        if expected not in names:
            print(f"   ADVARSEL: tabellen '{expected}' mangler")

    users = db.fetchone("SELECT COUNT(*) AS n FROM users")
    print(f"5. Brukere              ->  {users.get('n', 0) if users else 0}")

    subs = db.fetchone("SELECT COUNT(*) AS n FROM subscriptions")
    print(f"6. Abonnement           ->  {subs.get('n', 0) if subs else 0}")

    print("\nAlt ser OK ut. Registrering i appen skal naa virke mot denne databasen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
