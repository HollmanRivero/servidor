#!/usr/bin/env python3
"""Lag en engangskode for passordtilbakestilling.

Kjøres av administratoren på serveren. Serveren har ingen e-postutsending, så
koden må gis til brukeren på annen måte (SMS, Signal, si det ansikt til ansikt).

    cd backend && ../.venv/bin/python reset_password.py --user kari
    cd backend && ../.venv/bin/python reset_password.py --list
    cd backend && ../.venv/bin/python reset_password.py --user kari --set-passord nyttpassord

Engangskoden er gyldig i RESET_CODE_MINUTES minutter (standard 30), kan bare
brukes én gang, og lagres kun som hash.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth
import database


def main() -> int:
    args = sys.argv[1:]
    if not args or "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    database.init_db()

    if "--list" in args:
        users = auth.list_all_users()
        if not users:
            print("Ingen brukere i databasen.")
            return 0
        print(f"{'brukernavn':<24} {'rolle':<8} {'bannet'}")
        print("-" * 44)
        for u in users:
            print(f"{u['username']:<24} {u.get('role', ''):<8} {'ja' if u.get('is_banned') else 'nei'}")
        return 0

    if "--user" not in args:
        print("Mangler --user <brukernavn>. Se --help.")
        return 2
    username = args[args.index("--user") + 1]

    if "--set-passord" in args:
        new_password = args[args.index("--set-passord") + 1]
        if len(new_password) < 4:
            print("Passordet må ha minst 4 tegn.")
            return 2
        user = auth.get_user_by_username(username)
        if not user:
            print(f"Fant ingen bruker som heter '{username}'.")
            return 1
        database.get_db().execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (auth.hash_password(new_password), user["id"]),
        )
        print(f"Passordet til '{user['username']}' er satt direkte (uten engangskode).")
        return 0

    result = auth.create_reset_code(username)
    if not result:
        print(f"Fant ingen bruker som heter '{username}'. Bruk --list for å se brukerne.")
        return 1
    code, expires = result
    print()
    print(f"  Engangskode for {username}: {code}")
    print(f"  Gyldig til (UTC):            {expires}")
    print()
    print("  Brukeren skriver koden inn i appen: Logg inn -> Glemt passord?")
    print("  Koden kan bare brukes én gang.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
