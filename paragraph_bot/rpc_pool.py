"""Пул WebSocket-соединений к vshkole.net — по одному на владельца аккаунта,
независимо от того, с какой платформы (Telegram/MAX) пришла команда."""
from .rpc import AuthExpired, RpcClient
from .store import Store


class RpcPool:
    def __init__(self, cfg, store: Store) -> None:
        self._cfg = cfg
        self._store = store
        self._clients: dict[str, RpcClient] = {}

    async def get(self, owner_id: str) -> RpcClient:
        client = self._clients.get(owner_id)
        if client is None or not client.connected:
            acc = self._store.get_account(owner_id)
            if acc is None or not acc["token"]:
                raise AuthExpired("нет токена")
            client = RpcClient(self._cfg.rpc_url, acc["token"])
            await client.connect()
            self._clients[owner_id] = client
        return client

    async def drop(self, owner_id: str) -> None:
        client = self._clients.pop(owner_id, None)
        if client:
            await client.close()

    async def close_all(self) -> None:
        for c in self._clients.values():
            await c.close()
        self._clients.clear()
