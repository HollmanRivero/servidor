#!/usr/bin/env python3
"""CLI-verktøy for å sjekke og oppdatere feilloggen i SQL-databasen."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import database
import auth


def main() -> None:
    database.init_db()
    args = sys.argv[1:]
    if "--resolve-all" in args:
        auth.mark_errors_resolved("all")
        print("Markerte alle feilmeldinger som løst.")
        return

    only_unresolved = "--all" not in args
    errors = auth.list_error_logs(limit=100, only_unresolved=only_unresolved)
    if not errors:
        print("Ingen nye feilmeldinger i SQL-databasen.")
        return

    print(f"Fant {len(errors)} feilmelding(er) i SQL-databasen:\n")
    for err in errors:
        status = "LØST" if err["resolved"] else "ULØST"
        print(
            f"[#{err['id']}] [{status}] {err['created_at']} | "
            f"kilde={err['source']} | bruker={err['username']} | "
            f"endepunkt={err['endpoint']} | kode={err['status_code']}"
        )
        print(f"  Melding: {err['error_message']}")
        if err.get("details"):
            print(f"  Detaljer: {err['details'][:500]}")
        print("-" * 60)


if __name__ == "__main__":
    main()
