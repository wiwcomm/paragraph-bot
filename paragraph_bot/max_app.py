"""Транспорт MAX (platform-api2.max.ru) через библиотеку maxapi. Бизнес-логика
(checks/commands/store/rpc) общая с Telegram-ботом — меняется только приём и
отправка сообщений. Диалог /login (телефон → пароль) устроен так же, как в
Telegram: пароль используется один раз для signin, нигде не логируется и не
хранится, сообщение с паролем удаляется из чата сразу после получения.
"""
import asyncio
import logging
import re

from maxapi import Bot, Dispatcher
from maxapi.enums.parse_mode import TextFormat
from maxapi.filters.command import CommandStart
from maxapi.types.attachments.buttons.callback_button import CallbackButton
from maxapi.types.updates.bot_started import BotStarted
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.message_created import MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from . import checks, commands, config
from .linking import finish_login
from .notify import MAX_PREFIX
from .rpc import AuthExpired, RpcClient, RpcError
from .rpc_pool import RpcPool
from .store import Store

log = logging.getLogger("paragraph_bot.max")
PHONE_RE = re.compile(r"^\+?\d{10,15}$")


def _kb(rows: list[list[dict]]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.row(*(CallbackButton(text=b["text"], payload=b["callback_data"]) for b in row))
    return kb


def build(webhook_secret: str | None = None):
    """Возвращает (bot, dispatcher, store) — вызывающий код сам решает,
    poll'ить или поднимать webhook."""
    cfg = config.load()
    store = Store(cfg.database_url)
    bot = Bot(token=cfg.max_bot_token, format=TextFormat.HTML)
    dp = Dispatcher()
    pool = RpcPool(cfg, store)

    def oid(user_id: int) -> str:
        return f"{MAX_PREFIX}{user_id}"

    async def send(user_id: int, text: str, rows: list[list[dict]] | None = None) -> None:
        kwargs = {"attachments": [_kb(rows).as_markup()]} if rows is not None else {}
        await bot.send_message(user_id=user_id, text=text, **kwargs)

    async def do_login(user_id: int, phone: str, password: str) -> None:
        owner_id = oid(user_id)
        store.clear_session(owner_id)
        client = RpcClient(cfg.rpc_url, None)
        try:
            await client.connect_anonymous()
            res = await client.signin(phone, password)
        except (RpcError, OSError, asyncio.TimeoutError):
            log.warning("неудачный вход для MAX user_id=%s", user_id)  # без телефона/пароля
            await send(user_id, "❌ Не удалось войти: проверьте телефон и пароль. /login — попробовать снова.")
            await client.close()
            return
        linked_to = await finish_login(store, client, owner_id, res["token"])
        await client.close()
        if linked_to:
            await send(
                user_id,
                "✅ Этот логин уже привязан к другому чату (Telegram/MAX) — объединил доступ, "
                "настройки и уведомления теперь общие.",
                commands.MAIN_KEYBOARD["inline_keyboard"],
            )
        else:
            store.update_account(owner_id, auth_warned=0)
            await send(user_id, "✅ Вход выполнен! Теперь доступны все команды.", commands.MAIN_KEYBOARD["inline_keyboard"])

    async def send_settings(user_id: int) -> None:
        owner_id = oid(user_id)
        acc = store.get_account(owner_id)
        if acc is None:
            await send(user_id, "Настройки доступны только владельцу аккаунта. /login — войти.")
            return
        text = (
            "<b>Настройки</b>\n"
            f"Порог низкого баланса: {checks.rub(acc['low_balance_kopecks'])}\n"
            f"Интервал проверки: {acc['poll_seconds'] // 60} мин\n"
            f"Уведомления: {'🟢 включены' if acc['notify'] else '🔴 выключены'}"
        )
        await send(user_id, text, commands.settings_keyboard(acc)["inline_keyboard"])

    async def handle_set(user_id: int, args: list[str]) -> None:
        owner_id = oid(user_id)
        if store.get_account(owner_id) is None or len(args) < 2:
            return
        field, value = args[0], args[1]
        if field == "poll":
            store.update_account(owner_id, poll_seconds=int(value))
        elif field == "threshold":
            store.update_account(owner_id, low_balance_kopecks=int(value), balance_low=0)
        elif field == "notify":
            store.update_account(owner_id, notify=int(value))
        else:
            return
        await send_settings(user_id)

    async def handle(user_id: int, cmd: str, args: list[str]) -> None:
        owner_id = oid(user_id)
        cmd = cmd.lower()

        if cmd in ("help", "start"):
            await send(user_id, commands.HELP, commands.MAIN_KEYBOARD["inline_keyboard"])
            return
        if cmd == "login":
            store.set_session(owner_id, "login_phone")
            await send(user_id, "Отправьте номер телефона для входа (например +79991234567). /cancel — отменить.")
            return
        if cmd == "settings":
            await send_settings(user_id)
            return
        if cmd == "set":
            await handle_set(user_id, args)
            return
        if cmd == "share":
            if store.get_account(owner_id) is None:
                await send(user_id, "Сначала войдите в свой аккаунт: /login")
                return
            code = store.create_share_code(owner_id)
            await send(
                user_id,
                f"Код для привязки: <code>{code}</code>\nДействует 15 минут. "
                f"Другой человек должен написать боту:\n<code>/link {code}</code>",
            )
            return
        if cmd == "link":
            if not args:
                await send(user_id, "Использование: /link КОД")
                return
            owner = store.redeem_share_code(args[0], owner_id)
            if owner is None:
                await send(user_id, "Код неверный или истёк. Попросите прислать новый через /share.")
                return
            await send(
                user_id,
                "✅ Доступ привязан — теперь видите этот аккаунт и будете получать уведомления.",
                commands.MAIN_KEYBOARD["inline_keyboard"],
            )
            return
        if cmd == "unlink":
            store.remove_share(owner_id)
            await send(user_id, "Отвязано.", commands.MAIN_KEYBOARD["inline_keyboard"])
            return
        if cmd == "viewers":
            if store.get_account(owner_id) is None:
                await send(user_id, "Сначала войдите: /login")
                return
            viewers = store.list_viewers(owner_id)
            text = "Подключены: " + ", ".join(viewers) if viewers else "Пока никто не подключён. /share — дать доступ."
            await send(user_id, text)
            return

        resolved = store.resolve_owner(owner_id)
        if resolved is None:
            await send(
                user_id,
                "У вас нет привязанного аккаунта.\n/login — войти самому\n"
                "/link КОД — привязаться к чужому (например, мужа/жены)",
                commands.MAIN_KEYBOARD["inline_keyboard"],
            )
            return
        dispatch_handler = commands.DISPATCH.get(cmd)
        if not dispatch_handler:
            await send(user_id, "Неизвестная команда. /help — список.", commands.MAIN_KEYBOARD["inline_keyboard"])
            return
        try:
            rpc = await pool.get(resolved)
            reply = await dispatch_handler(rpc, store, resolved, args)
            if cmd == "status" and resolved != owner_id:
                reply += "\n\n🔗 Это общий доступ: вы смотрите чужой аккаунт (владелец вошёл в другом чате/платформе)."
        except AuthExpired:
            reply = (
                "🔑 Сессия истекла. Выполните /login"
                if resolved == owner_id
                else "🔑 Сессия владельца истекла. Попросите его выполнить /login заново."
            )
        except Exception:
            log.exception("команда /%s упала", cmd)
            reply = "⚠️ Ошибка при выполнении команды"
        rows = (
            commands.menu_keyboard(commands.week_from_args(args))["inline_keyboard"]
            if cmd == "menu"
            else commands.MAIN_KEYBOARD["inline_keyboard"]
        )
        await send(user_id, reply, rows)

    @dp.bot_started()
    async def on_started(event: BotStarted) -> None:
        await send(event.user.user_id, commands.HELP, commands.MAIN_KEYBOARD["inline_keyboard"])

    @dp.message_created(CommandStart())
    async def on_start(event: MessageCreated) -> None:
        await send(event.from_user.user_id, commands.HELP, commands.MAIN_KEYBOARD["inline_keyboard"])

    @dp.message_created()
    async def on_message(event: MessageCreated) -> None:
        body = event.message.body if event.message else None
        text = (body.text or "").strip() if body else ""
        if not text or not event.from_user:
            return
        user_id = event.from_user.user_id
        owner_id = oid(user_id)

        session = store.get_session(owner_id)
        if session is not None:
            state, data = session
            if text == "/cancel":
                store.clear_session(owner_id)
                await send(user_id, "Отменено.", commands.MAIN_KEYBOARD["inline_keyboard"])
                return
            if state == "login_phone":
                phone = text.replace(" ", "")
                if not PHONE_RE.match(phone):
                    await send(user_id, "Похоже, это не номер телефона. Пример: +79991234567. /cancel — отменить.")
                    return
                store.set_session(owner_id, "login_password", phone)
                await send(user_id, "Теперь отправьте пароль. Сообщение будет сразу удалено из чата.")
                return
            if state == "login_password":
                if body and body.mid:
                    try:
                        await bot.delete_message(body.mid)
                    except Exception:
                        pass
                await do_login(user_id, data, text)
                return

        if not text.startswith("/"):
            return
        parts = text[1:].split()
        await handle(user_id, parts[0], parts[1:])

    @dp.message_callback()
    async def on_callback(event: MessageCallback) -> None:
        payload = event.callback.payload if event.callback else None
        await event.answer()
        if not payload:
            return
        user = event.callback.user if event.callback else None
        user_id = user.user_id if user else (event.from_user.user_id if event.from_user else None)
        if user_id is None:
            return
        parts = payload.split()
        if parts:
            await handle(user_id, parts[0], parts[1:])

    return bot, dp, store, pool
