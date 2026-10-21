"""Маршрутизация фоновых уведомлений (баланс/операции/меню) по платформе.
owner_id с префиксом "max:" уходит в MAX, обычный числовой chat_id — в Telegram,
как было раньше. Это единственное место, которое знает про обе платформы."""
import logging

log = logging.getLogger("paragraph_bot.notify")

MAX_PREFIX = "max:"


class MultiNotifier:
    def __init__(self, telegram, max_bot=None) -> None:
        self._telegram = telegram
        self._max_bot = max_bot

    async def send(self, owner_id: str, text: str) -> None:
        if owner_id.startswith(MAX_PREFIX):
            if self._max_bot is None:
                log.warning("уведомление для %s пропущено — MAX бот не настроен", owner_id)
                return
            from maxapi.enums.parse_mode import TextFormat

            await self._max_bot.send_message(
                user_id=int(owner_id[len(MAX_PREFIX):]), text=text, format=TextFormat.HTML
            )
            return
        await self._telegram.send(owner_id, text)
