"""Клиент WebSocket-RPC платформы «Параграф» (wss://api.vshkole.net/rpc).

Кадр запроса: {"id": n, "method": "...", "params": {...}}.
Ответ: {"id": n, "result": ...} или {"id": n, "error": ...}. Кадры без id — уведомления (push).
Два способа аутентификации:
  - connect() + token → resume (постоянная работа; токен уже выдан раньше).
  - connect_anonymous() + signin(phone, password) → новый токен (разовый вход пользователя,
    пароль этим клиентом нигде не сохраняется, только передаётся один раз на сервер).
"""
import asyncio
import itertools
import json

import websockets

ORIGIN = "https://lk.vshkole.net"


class RpcError(Exception):
    pass


class AuthExpired(RpcError):
    """Токен не принят (resume отказал) — нужен повторный вход (signin)."""


class RpcClient:
    def __init__(self, url: str, token: str | None) -> None:
        self._url = url
        self._token = token
        self._ws = None
        self._ids = itertools.count(1)

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

    async def connect(self) -> None:
        """Вход по уже имеющемуся токену (resume)."""
        if not self._token:
            raise AuthExpired("токена нет")
        await self.connect_anonymous()
        try:
            await self.call("resume", {"token": self._token})
        except RpcError as e:
            await self.close()
            raise AuthExpired("токен не принят — нужен повторный вход") from e

    async def connect_anonymous(self) -> None:
        """Открывает соединение без аутентификации — перед signin."""
        self._ws = await websockets.connect(
            self._url, origin=ORIGIN, open_timeout=15, ping_interval=20
        )

    async def signin(self, phone: str, password: str) -> dict:
        """Новый вход по телефону/паролю. Возвращает {token, user}."""
        return await self.call("signin", {"phone": phone, "password": password})

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def call(self, method: str, params: dict | None = None, timeout: float = 20):
        req_id = next(self._ids)
        await self._ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
        async with asyncio.timeout(timeout):
            while True:
                msg = json.loads(await self._ws.recv())
                if msg.get("id") != req_id:
                    continue  # push-кадры и чужие ответы пропускаем
                if msg.get("error"):
                    raise RpcError(f"{method}: {msg['error']}")
                return msg.get("result")
