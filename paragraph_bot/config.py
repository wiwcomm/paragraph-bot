import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Config:
    telegram_token: str
    admin_chat_id: str | None  # куда слать уведомления уровня бота (не обязателен)
    max_bot_token: str | None  # None — MAX-бот выключен
    max_webhook_secret: str | None
    max_webhook_url: str | None
    database_url: str
    rpc_url: str = "wss://api.vshkole.net/rpc"


def load() -> Config:
    return Config(
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        admin_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        max_bot_token=os.environ.get("MAX_BOT_TOKEN") or None,
        max_webhook_secret=os.environ.get("MAX_WEBHOOK_SECRET") or None,
        max_webhook_url=os.environ.get("MAX_WEBHOOK_URL") or None,
        database_url=os.environ["DATABASE_URL"],
    )
