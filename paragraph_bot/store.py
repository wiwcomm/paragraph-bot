"""Мультипользовательское хранилище — PostgreSQL (psycopg3, синхронный клиент,
как и раньше с sqlite3: вызовы блокирующие, но короткие, вызываются внутри
async-функций так же, как это было организовано на SQLite).

- accounts: один владелец (chat_id, с префиксом "max:" для MAX) = один вход в
  vshkole.net, свой токен, свои настройки (порог баланса, интервал опроса,
  уведомления вкл/выкл). vshkole_id — внутренний _id профиля на vshkole.net,
  по нему распознаём повторный вход того же человека с другой платформы.
- shares: viewer_id -> owner_id — другой чат видит данные и уведомления
  владельца без собственного входа (жена видит аккаунт мужа, или тот же человек
  вошёл с тем же логином на другой платформе — тогда привязка автоматическая).
- share_codes: одноразовые короткоживущие коды для самостоятельной привязки (/share → /link).
- sessions: состояние многошаговых диалогов (например /login: ждём телефон, потом пароль).
- menu_hashes, kv: вспомогательное.
"""
import secrets
import time

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS accounts (
    owner_id TEXT PRIMARY KEY,
    token TEXT,
    vshkole_id TEXT,
    low_balance_kopecks INTEGER NOT NULL DEFAULT 30000,
    poll_seconds INTEGER NOT NULL DEFAULT 300,
    notify INTEGER NOT NULL DEFAULT 1,
    payment_cursor TEXT,
    balance_low INTEGER NOT NULL DEFAULT 0,
    auth_warned INTEGER NOT NULL DEFAULT 0,
    last_checked DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS menu_hashes (
    owner_id TEXT NOT NULL,
    key TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (owner_id, key)
);

CREATE TABLE IF NOT EXISTS shares (
    viewer_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS share_codes (
    code TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    chat_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data TEXT
);
"""


class Store:
    def __init__(self, dsn: str) -> None:
        self._db = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self._db.execute(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Добавляет новые колонки к уже существующей базе (CREATE TABLE IF NOT
        EXISTS их не создаст, если таблица была заведена более ранней версией)."""
        cols = {
            r["column_name"]
            for r in self._db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'accounts'"
            ).fetchall()
        }
        if "vshkole_id" not in cols:
            self._db.execute("ALTER TABLE accounts ADD COLUMN vshkole_id TEXT")

    # --- простые k/v (сейчас только tg_offset) ---
    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT v FROM kv WHERE k = %s", (key,)).fetchone()
        return row["v"] if row else None

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO kv (k, v) VALUES (%s, %s) "
            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
            (key, value),
        )

    # --- accounts ---
    def create_account(self, owner_id: str, token: str, vshkole_id: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO accounts (owner_id, token, vshkole_id, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (owner_id) DO UPDATE SET token = EXCLUDED.token, "
            "vshkole_id = COALESCE(EXCLUDED.vshkole_id, accounts.vshkole_id)",
            (owner_id, token, vshkole_id, time.time()),
        )

    def find_account_by_vshkole_id(self, vshkole_id: str, exclude_owner_id: str) -> str | None:
        """Ищет УЖЕ существующий аккаунт (на любой платформе) с тем же внутренним
        _id профиля vshkole.net — чтобы не плодить вторую независимую сессию для
        того же человека. exclude_owner_id — не считать совпадением самого себя."""
        row = self._db.execute(
            "SELECT owner_id FROM accounts WHERE vshkole_id = %s AND owner_id != %s LIMIT 1",
            (vshkole_id, exclude_owner_id),
        ).fetchone()
        return row["owner_id"] if row else None

    def get_account(self, owner_id: str) -> dict | None:
        return self._db.execute(
            "SELECT * FROM accounts WHERE owner_id = %s", (owner_id,)
        ).fetchone()

    def all_accounts(self) -> list[dict]:
        return self._db.execute("SELECT * FROM accounts").fetchall()

    def update_account(self, owner_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = %s" for k in fields)
        self._db.execute(
            f"UPDATE accounts SET {cols} WHERE owner_id = %s",
            (*fields.values(), owner_id),
        )

    def delete_account(self, owner_id: str) -> None:
        self._db.execute("DELETE FROM accounts WHERE owner_id = %s", (owner_id,))
        self._db.execute("DELETE FROM menu_hashes WHERE owner_id = %s", (owner_id,))
        self._db.execute("DELETE FROM shares WHERE owner_id = %s", (owner_id,))

    # --- menu hashes ---
    def get_menu_hash(self, owner_id: str, key: str) -> str | None:
        row = self._db.execute(
            "SELECT hash FROM menu_hashes WHERE owner_id = %s AND key = %s", (owner_id, key)
        ).fetchone()
        return row["hash"] if row else None

    def set_menu_hash(self, owner_id: str, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO menu_hashes (owner_id, key, hash) VALUES (%s, %s, %s) "
            "ON CONFLICT (owner_id, key) DO UPDATE SET hash = EXCLUDED.hash",
            (owner_id, key, value),
        )

    # --- sharing ---
    def resolve_owner(self, chat_id: str) -> str | None:
        """Свой аккаунт в приоритете, иначе — чей аккаунт расшарен на этот чат."""
        if self.get_account(chat_id) is not None:
            return chat_id
        row = self._db.execute(
            "SELECT owner_id FROM shares WHERE viewer_id = %s", (chat_id,)
        ).fetchone()
        return row["owner_id"] if row else None

    def create_share_code(self, owner_id: str, ttl_seconds: int = 900) -> str:
        code = secrets.token_hex(3).upper()  # 6 символов
        self._db.execute(
            "INSERT INTO share_codes (code, owner_id, expires_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (code) DO UPDATE SET owner_id = EXCLUDED.owner_id, "
            "expires_at = EXCLUDED.expires_at",
            (code, owner_id, time.time() + ttl_seconds),
        )
        return code

    def redeem_share_code(self, code: str, viewer_id: str) -> str | None:
        row = self._db.execute(
            "SELECT owner_id, expires_at FROM share_codes WHERE code = %s", (code.upper(),)
        ).fetchone()
        if not row or row["expires_at"] < time.time():
            return None
        owner_id = row["owner_id"]
        self._db.execute("DELETE FROM share_codes WHERE code = %s", (code.upper(),))
        self.add_share(owner_id, viewer_id)
        return owner_id

    def add_share(self, owner_id: str, viewer_id: str) -> None:
        """Прямая привязка без кода — используется при автосвязке по vshkole_id."""
        self._db.execute(
            "INSERT INTO shares (viewer_id, owner_id, created_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (viewer_id) DO UPDATE SET owner_id = EXCLUDED.owner_id, "
            "created_at = EXCLUDED.created_at",
            (viewer_id, owner_id, time.time()),
        )

    def list_viewers(self, owner_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT viewer_id FROM shares WHERE owner_id = %s", (owner_id,)
        ).fetchall()
        return [r["viewer_id"] for r in rows]

    def remove_share(self, viewer_id: str) -> None:
        self._db.execute("DELETE FROM shares WHERE viewer_id = %s", (viewer_id,))

    # --- sessions (многошаговые диалоги, напр. /login) ---
    def get_session(self, chat_id: str) -> tuple[str, str | None] | None:
        row = self._db.execute(
            "SELECT state, data FROM sessions WHERE chat_id = %s", (chat_id,)
        ).fetchone()
        return (row["state"], row["data"]) if row else None

    def set_session(self, chat_id: str, state: str, data: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO sessions (chat_id, state, data) VALUES (%s, %s, %s) "
            "ON CONFLICT (chat_id) DO UPDATE SET state = EXCLUDED.state, data = EXCLUDED.data",
            (chat_id, state, data),
        )

    def clear_session(self, chat_id: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE chat_id = %s", (chat_id,))
