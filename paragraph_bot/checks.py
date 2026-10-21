"""Проверки для одного аккаунта (owner_id). Каждая возвращает список текстов для
Telegram и обновляет состояние в Store. Поля подтверждены на реальном ответе
сервера (probe + ручная проверка 2026-10-21): balance.parent.amount — строка
(копейки); transactions.payment[] несёт название операции в dsc.transactionName;
get_home.scholars[] не даёт готового ФИО, только firstName/secondName/middleName.
"""
from .rpc import RpcClient
from .store import Store


def rub(kopecks: int | float) -> str:
    return f"{kopecks / 100:.2f} ₽"


def _payments(res) -> list[dict]:
    if isinstance(res, dict):
        res = res.get("payment") or res.get("payments") or res.get("items") or []
    return res if isinstance(res, list) else []


def _pid(op: dict) -> int:
    return int(op.get("payment_id") or op.get("paymentId") or op.get("id") or 0)


def _full_name(kid: dict) -> str:
    parts = [kid.get("firstName"), kid.get("secondName"), kid.get("middleName")]
    return " ".join(p for p in parts if p) or kid.get("_id", "ребёнок")


def _format_op(op: dict) -> str:
    amount = op.get("amount") or 0
    dsc = op.get("dsc") or {}
    title = dsc.get("transactionName") or op.get("terminal_name") or "Операция"
    is_topup = "пополн" in title.lower()
    sign = "➕" if is_topup else "🍽"
    return f"{sign} {title}: <b>{rub(abs(amount))}</b>"


async def check_balance(rpc: RpcClient, store: Store, owner_id: str, low: int) -> list[str]:
    res = await rpc.call("balance")
    if not store.get_account(owner_id)["vshkole_id"] and res.get("_id"):
        # дозаполнение для аккаунтов, созданных до появления автосвязки по логину
        # (например, мигрированных из старой однопользовательской версии)
        store.update_account(owner_id, vshkole_id=res["_id"])
    raw = (res.get("parent") or res).get("amount")
    if raw is None:
        return []
    amount = int(float(raw))  # amount приходит строкой
    was_low = bool(store.get_account(owner_id)["balance_low"])
    is_low = amount < low
    store.update_account(owner_id, balance_low=int(is_low))
    if is_low and not was_low:  # шлём один раз при пересечении порога
        return [f"⚠️ Низкий баланс: <b>{rub(amount)}</b> (порог {rub(low)})"]
    return []


async def check_transactions(rpc: RpcClient, store: Store, owner_id: str) -> list[str]:
    cursor = store.get_account(owner_id)["payment_cursor"]
    if cursor is None:  # первый запуск: запоминаем последнюю операцию, не шумим историей
        ops = _payments(await rpc.call("transactions", {"sort_type": "desc", "count": 1}))
        store.update_account(owner_id, payment_cursor=str(max((_pid(o) for o in ops), default=0)))
        return []
    if cursor == "0":
        ops = _payments(await rpc.call("transactions", {"sort_type": "desc", "count": 20}))
    else:
        res = await rpc.call("transactions_since", {"payment_id": cursor})
        ops = _payments(res)
    if not ops:
        return []
    store.update_account(owner_id, payment_cursor=str(max(_pid(o) for o in ops)))
    return [_format_op(o) for o in reversed(ops)]  # старые первыми


async def check_menu(rpc: RpcClient, store: Store, owner_id: str) -> list[str]:
    import hashlib
    import json

    home = await rpc.call("get_home")
    out: list[str] = []
    for kid in home.get("scholars") or []:
        sid = kid["_id"]
        name = _full_name(kid)
        for week in ("current", "next"):
            menu = await rpc.call("week_menu", {"scholarId": sid, "week": week})
            digest = hashlib.sha256(json.dumps(menu, sort_keys=True).encode()).hexdigest()
            key = f"menu:{sid}:{week}"
            old = store.get_menu_hash(owner_id, key)
            store.set_menu_hash(owner_id, key, digest)
            if old is not None and old != digest:
                label = "текущую" if week == "current" else "следующую"
                out.append(f"📋 Изменилось меню на {label} неделю ({name})")
    return out
