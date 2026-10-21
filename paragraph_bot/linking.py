"""Общая логика после успешного signin (вызывается и из Telegram-, и из
MAX-бота): распознаём, не тот же ли это человек, что уже вошёл на другой
платформе — по внутреннему _id профиля vshkole.net (не по телефону: его сверять
ненадёжно — проверка идёт только когда человек САМ ввёл верный пароль и мы
получили настоящий _id с сервера). Если да — не плодим вторую независимую
RPC-сессию, а делаем этот чат «зрителем» уже существующего аккаунта, как при
/share, только автоматически."""
import logging

from .rpc import RpcClient
from .store import Store

log = logging.getLogger("paragraph_bot.linking")


async def finish_login(store: Store, rpc_client: RpcClient, owner_id: str, token: str) -> str | None:
    """Вызывать сразу после client.signin(...) на ТОМ ЖЕ соединении (токен уже
    действует на нём). Возвращает owner_id уже существующего аккаунта, если
    этот чат привязан к нему как зритель, иначе None (чат стал владельцем сам)."""
    vshkole_id = None
    try:
        res = await rpc_client.call("balance")
        vshkole_id = (res or {}).get("_id")
    except Exception:
        log.exception("не удалось получить _id профиля после входа")

    existing = store.find_account_by_vshkole_id(vshkole_id, owner_id) if vshkole_id else None
    if existing:
        store.add_share(existing, owner_id)
        return existing

    store.create_account(owner_id, token, vshkole_id=vshkole_id)
    return None
