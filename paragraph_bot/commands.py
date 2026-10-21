"""Команды Telegram. Каждая async-функция принимает (rpc, store, owner_id, args) и
возвращает готовый HTML-текст ответа — owner_id это ЧЕЙ аккаунт смотрим (свой или
тот, что расшарили), не обязательно chat_id вызвавшего. Формы ответов сверены с
реальным сервером (probe + ручная проверка) 2026-10-21.
"""
from datetime import date as _date
from datetime import datetime as _datetime

from .checks import _full_name, _payments, rub

HELP = (
    "<b>Команды</b>\n"
    "/balance — баланс кошелька\n"
    "/kids — дети и статус карт\n"
    "/menu [next] — меню на текущую или следующую неделю\n"
    "/transactions [N] — последние N операций (по умолч. 5, макс 20)\n"
    "/status — состояние аккаунта\n"
    "/settings — порог баланса, интервал опроса, уведомления\n"
    "/login — привязать свой аккаунт vshkole.net (если вы уже входили с этим же "
    "логином в Telegram или MAX — доступ объединится автоматически, без /share)\n"
    "/share — дать другому человеку доступ к вашему аккаунту (например жене)\n"
    "/link КОД — принять доступ, который вам прислали через /share\n"
    "/unlink — отвязаться от чужого аккаунта\n"
    "/viewers — кто подключён к вашему аккаунту\n"
    "/help — эта справка\n\n"
    "Или пользуйтесь кнопками ниже 👇"
)

DAY_NAMES = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
MEAL_ICONS = {"завтрак": "🍳", "обед": "🍲", "полдник": "🥐"}

MAIN_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "💰 Баланс", "callback_data": "balance"}, {"text": "👧 Дети", "callback_data": "kids"}],
        [{"text": "🍽 Меню", "callback_data": "menu"}, {"text": "💳 Операции", "callback_data": "transactions"}],
        [{"text": "⚙️ Настройки", "callback_data": "settings"}, {"text": "🔗 Поделиться", "callback_data": "share"}],
        [{"text": "ℹ️ Статус", "callback_data": "status"}, {"text": "🔑 Войти", "callback_data": "login"}],
    ]
}

POLL_PRESETS = [(60, "1 мин"), (300, "5 мин"), (900, "15 мин"), (1800, "30 мин")]
THRESHOLD_PRESETS = [(10000, "100 ₽"), (30000, "300 ₽"), (50000, "500 ₽"), (100000, "1000 ₽")]


def menu_keyboard(week: str) -> dict:
    other = "current" if week == "next" else "next"
    other_label = "◀ Текущая неделя" if week == "next" else "Следующая неделя ▶"
    return {
        "inline_keyboard": [
            [{"text": other_label, "callback_data": f"menu {other}"}],
            [{"text": "⬅️ В меню бота", "callback_data": "start"}],
        ]
    }


def settings_keyboard(acc) -> dict:
    poll_row = [
        {"text": ("✅ " if acc["poll_seconds"] == v else "") + label, "callback_data": f"set poll {v}"}
        for v, label in POLL_PRESETS
    ]
    threshold_row = [
        {"text": ("✅ " if acc["low_balance_kopecks"] == v else "") + label, "callback_data": f"set threshold {v}"}
        for v, label in THRESHOLD_PRESETS
    ]
    notify_label = "🔕 Выключить уведомления" if acc["notify"] else "🔔 Включить уведомления"
    return {
        "inline_keyboard": [
            poll_row[:2],
            poll_row[2:],
            threshold_row[:2],
            threshold_row[2:],
            [{"text": notify_label, "callback_data": f"set notify {0 if acc['notify'] else 1}"}],
            [{"text": "⬅️ В меню бота", "callback_data": "start"}],
        ]
    }


def week_from_args(args: list[str]) -> str:
    return "next" if args and args[0].lower() in ("next", "след", "следующая") else "current"


async def cmd_balance(rpc, store, owner_id, args) -> str:
    res = await rpc.call("balance")
    amount = int(float((res.get("parent") or res)["amount"]))
    return f"Баланс: <b>{rub(amount)}</b>"


async def cmd_kids(rpc, store, owner_id, args) -> str:
    home = await rpc.call("get_home")
    kids = home.get("scholars") or []
    if not kids:
        return "Дети не привязаны."
    lines = []
    for k in kids:
        school = (k.get("school") or {}).get("title", "—")
        klass = k.get("class") or {}
        cls = f"{klass.get('number', '?')}{klass.get('literal', '')}"
        status = "🟢 активна" if k.get("cardStatus") else "🔴 заблокирована"
        lines.append(f"<b>{_full_name(k)}</b>, {cls} класс, {school}\nКарта: {status}")
    return "\n\n".join(lines)


async def cmd_menu(rpc, store, owner_id, args) -> str:
    week = week_from_args(args)
    home = await rpc.call("get_home")
    kids = home.get("scholars") or []
    if not kids:
        return "Дети не привязаны."
    out = []
    for k in kids:
        menu = await rpc.call("week_menu", {"scholarId": k["_id"], "week": week})
        out.append(f"<b>{_full_name(k)}</b>\n\n{_format_menu(menu)}")
    return "\n\n".join(out)


def _format_menu(menu: dict) -> str:
    days = menu.get("days") or []
    if not days:
        return "меню пусто"
    blocks = []
    for day in days:
        try:
            d = _date.fromisoformat(day["date"])
            label = f"{DAY_NAMES.get(d.weekday(), '')} {d.strftime('%d.%m')}"
        except Exception:
            label = day.get("date", "?")
        rows = []
        for meal, variants in (day.get("meals") or {}).items():
            opt = (variants or {}).get("paid") or (variants or {}).get("free")
            if not opt:
                continue
            icon = MEAL_ICONS.get(meal, "🍽")
            dishes = ", ".join(dsh["name"] for dsh in opt.get("dishes") or [])
            cap = meal.capitalize()
            rows.append(f"{icon} {cap}: {rub(opt.get('cost', 0))} — {dishes}")
        if rows:
            blocks.append(f"<b>{label}</b>\n" + "\n".join(rows))
    return "\n\n".join(blocks) if blocks else "меню пусто"


STATUS_OK = {"принят", "кошелек пополнен"}


def _tx_name(op: dict) -> str:
    dsc = op.get("dsc") or {}
    return dsc.get("name") or dsc.get("transactionName") or op.get("terminal_name") or "Операция"


def _tx_is_topup(op: dict) -> bool:
    dsc = op.get("dsc") or {}
    text = (dsc.get("transactionName") or "") + (op.get("status") or "")
    return "пополн" in text.lower()


def _format_transactions(ops: list[dict]) -> str:
    days: dict[str, dict] = {}
    order: list[str] = []
    for op in ops:
        raw = op.get("date") or ""
        try:
            dt = _datetime.strptime(raw, "%d.%m.%Y %H:%M:%S")
            key = dt.strftime("%Y-%m-%d")
            label = f"{DAY_NAMES.get(dt.weekday(), '')} {dt.strftime('%d.%m')}"
            time_label = dt.strftime("%H:%M")
        except ValueError:
            key = label = raw or "?"
            time_label = ""
        if key not in days:
            days[key] = {"label": label, "lines": []}
            order.append(key)

        amount = op.get("amount") or 0
        icon = "➕" if _tx_is_topup(op) else "🍽"
        status = (op.get("status") or "").strip()
        badge = "" if not status or status.lower() in STATUS_OK else f" <i>({status})</i>"
        days[key]["lines"].append(
            f"{time_label} {icon} {_tx_name(op)} — <b>{rub(abs(amount))}</b>{badge}"
        )

    return "\n\n".join(f"<b>{d['label']}</b>\n" + "\n".join(d["lines"]) for d in (days[k] for k in order))


async def cmd_transactions(rpc, store, owner_id, args) -> str:
    count = 10
    if args and args[0].isdigit():
        count = max(1, min(int(args[0]), 30))
    ops = _payments(await rpc.call("transactions", {"sort_type": "desc", "count": count}))
    if not ops:
        return "Операций нет."
    return _format_transactions(ops)


async def cmd_status(rpc, store, owner_id, args) -> str:
    acc = store.get_account(owner_id)
    auth = "🟢 вход активен" if rpc.connected else "🔴 нет соединения"
    viewers = store.list_viewers(owner_id)
    shared = f"\nОбщий доступ: {len(viewers)} чел." if viewers else ""
    return (
        f"{auth}\n"
        f"Порог низкого баланса: {rub(acc['low_balance_kopecks'])}\n"
        f"Опрос: раз в {acc['poll_seconds'] // 60} мин\n"
        f"Уведомления: {'включены' if acc['notify'] else 'выключены'}{shared}"
    )


DISPATCH = {
    "balance": cmd_balance,
    "kids": cmd_kids,
    "menu": cmd_menu,
    "transactions": cmd_transactions,
    "status": cmd_status,
}
