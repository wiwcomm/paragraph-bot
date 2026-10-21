import json

import httpx


class Telegram:
    """Не привязан к одному chat_id — бот обслуживает любое число пользователей,
    каждый вызов явно указывает, кому отправить."""

    def __init__(self, token: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = httpx.AsyncClient(timeout=15)

    async def send(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        r = await self._http.post(f"{self._base}/sendMessage", json=payload)
        r.raise_for_status()
        return r.json()["result"]

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        """Используется сразу после того, как пользователь прислал пароль в чат —
        убрать его из истории. Не критично, если не получится (бот пароль не хранит
        и не логирует в любом случае), поэтому ошибки тут проглатываются."""
        try:
            await self._http.post(
                f"{self._base}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            )
        except Exception:
            pass

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        r = await self._http.post(f"{self._base}/answerCallbackQuery", json=payload)
        r.raise_for_status()

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params: dict = {"timeout": timeout, "allowed_updates": '["message","callback_query"]'}
        if offset is not None:
            params["offset"] = offset
        r = await self._http.get(
            f"{self._base}/getUpdates", params=params, timeout=timeout + 15
        )
        r.raise_for_status()
        return r.json()["result"]

    async def close(self) -> None:
        await self._http.aclose()
