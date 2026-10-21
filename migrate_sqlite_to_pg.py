"""Разовый перенос данных из старого state.db (SQLite) в PostgreSQL.
Запуск: uv run python migrate_sqlite_to_pg.py /path/to/state.db
"""
import sqlite3
import sys

from paragraph_bot.config import load
from paragraph_bot.store import Store


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("использование: migrate_sqlite_to_pg.py /path/to/state.db")
    sqlite_path = sys.argv[1]

    old = sqlite3.connect(sqlite_path)
    old.row_factory = sqlite3.Row

    cfg = load()
    new = Store(cfg.database_url)

    n = 0
    for row in old.execute("SELECT * FROM accounts"):
        new._db.execute(
            "INSERT INTO accounts (owner_id, token, vshkole_id, low_balance_kopecks, "
            "poll_seconds, notify, payment_cursor, balance_low, auth_warned, last_checked, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (owner_id) DO UPDATE SET token = EXCLUDED.token",
            (
                row["owner_id"], row["token"], row["vshkole_id"] if "vshkole_id" in row.keys() else None,
                row["low_balance_kopecks"], row["poll_seconds"], row["notify"], row["payment_cursor"],
                row["balance_low"], row["auth_warned"], row["last_checked"], row["created_at"],
            ),
        )
        n += 1
    print(f"accounts: {n}")

    n = 0
    for row in old.execute("SELECT * FROM shares"):
        new.add_share(row["owner_id"], row["viewer_id"])
        n += 1
    print(f"shares: {n}")

    n = 0
    for row in old.execute("SELECT * FROM menu_hashes"):
        new.set_menu_hash(row["owner_id"], row["key"], row["hash"])
        n += 1
    print(f"menu_hashes: {n}")

    n = 0
    for row in old.execute("SELECT * FROM kv"):
        new.set(row["k"], row["v"])
        n += 1
    print(f"kv: {n}")

    print("done")


if __name__ == "__main__":
    main()
