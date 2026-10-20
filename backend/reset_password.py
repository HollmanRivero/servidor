#!/usr/bin/env python3
"""Lag en engangskode for passordtilbakestilling.

Kjores av administratoren pa serveren. Serveren har ingen e-postutsending, sa
koden ma gis til brukeren pa annen mate (SMS, Signal, si det ansikt til ansikt).

    cd ~/Dokumenter/MyGits/servidor/servidor          (repo-roten, ikke backend/)
    .venv/bin/python backend/reset_password.py --user kari
    .venv/bin/python backend/reset_password.py --user "Hollman Rivero"
    .venv/bin/python backend/reset_password.py --list
    .venv/bin/python backend/reset_password.py --find kari
    .venv/bin/python backend/reset_password.py --user kari --set-passord nyttpassord

Bruk ALLTID prosjektets .venv. System-python mangler pakkene, og sudo gir deg en
annen brukers hjemmemappe og feil database.

Tips: skriv stien uten ".." (altsa fra repo-roten). "cd backend" + "../.venv/bin/python"
virker, men Python 3.14 skriver da to RuntimeWarning-linjer om sys.prefix fordi
".." staar i stien. Det er ikke en feil i skriptet, bare stoy.

Engangskoden er gyldig i RESET_CODE_MINUTES minutter (standard 30), kan bare
brukes en gang, og lagres kun som hash.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import auth
    import database
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"\nMangler Python-pakken '{exc.name}'.\n"
        "Du kjorer system-python (eller sudo) i stedet for prosjektets .venv.\n\n"
        "  Riktig (fra repo-roten):\n"
        "      .venv/bin/python backend/reset_password.py --user \"Ditt Navn\"\n\n"
        "  Feil (virker ikke):\n"
        "      sudo python3 reset_password.py --user Ditt Navn\n"
    ) from None


def _value_of(args: list[str], flag: str) -> str:
    """Verdien etter et flagg, ogsaa naar den inneholder mellomrom.

    '--user Hollman Rivero' gir 'Hollman Rivero' (stopper ved neste --flagg),
    slik at brukernavn med mellomrom virker uten anforselstegn."""
    if flag not in args:
        return ""
    words: list[str] = []
    for arg in args[args.index(flag) + 1:]:
        if arg.startswith("--"):
            break
        words.append(arg)
    return " ".join(words).strip()


def _print_users(users: list[dict]) -> None:
    print(f"{'brukernavn':<24} {'rolle':<8} {'bannet':<7} opprettet")
    print("-" * 62)
    for u in users:
        print(
            f"{u.get('username', ''):<24} {u.get('role', ''):<8} "
            f"{('ja' if u.get('is_banned') else 'nei'):<7} {u.get('created_at', '')}"
        )


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
        _print_users(users)
        return 0

    if "--find" in args:
        text = _value_of(args, "--find")
        if not text:
            print("Bruk: --find <tekst>")
            return 2
        hits = [u for u in auth.list_all_users() if text.lower() in str(u.get("username", "")).lower()]
        if not hits:
            print(f"Ingen brukere inneholder '{text}'.")
            return 1
        _print_users(hits)
        return 0

    if "--user" not in args:
        print("Mangler --user <brukernavn>. Se --help.")
        return 2

    username = _value_of(args, "--user")
    if not username:
        print("Mangler brukernavn etter --user.")
        return 2

    user = auth.get_user_by_username(username)
    if not user:
        print(f"Fant ingen bruker som heter '{username}'.")
        print("Bruk --list for å se brukerne. Husk anførselstegn hvis navnet har mellomrom.")
        return 1

    if "--set-passord" in args:
        new_password = _value_of(args, "--set-passord")
        if len(new_password) < 4:
            print("Passordet må ha minst 4 tegn.")
            return 2
        database.get_db().execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (auth.hash_password(new_password), user["id"]),
        )
        print(f"Passordet til '{user['username']}' er satt direkte (uten engangskode).")
        return 0

    result = auth.create_reset_code(user["username"])
    if not result:
        print(f"Kunne ikke lage kode for '{username}'.")
        return 1
    code, expires = result
    print()
    print(f"  Engangskode for {user['username']}: {code}")
    print(f"  Gyldig til (UTC):                  {expires}")
    print()
    print("  Brukeren skriver koden inn i appen: Logg inn -> Glemt passord?")
    print("  Koden kan bare brukes én gang.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
