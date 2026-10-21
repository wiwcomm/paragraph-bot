"""Запуск: uv run python -m paragraph_bot [run|once|listen|max-listen|probe|tg-test]

Мультипользовательский бот, две платформы (Telegram и MAX) на общей бизнес-логике
и общей БД: каждый чат либо владеет своим аккаунтом vshkole.net (вошёл через
/login), либо смотрит чужой через /link КОД (/share у владельца). owner_id для
MAX хранится с префиксом "max:", чтобы не пересечься с telegram chat_id.
Процессы: run (фоновые проверки, обе платформы), listen (Telegram getUpdates),
max-listen (MAX webhook) — слушатели раздельные, т.к. это разные протоколы.
"""
import asyncio
import json
import logging
import re
import sys
import time

from . import checks, commands, config
from .linking import finish_login
from .notify import MultiNotifier
from .rpc import AuthExpired, RpcClient, RpcError
from .rpc_pool import RpcPool
from .store import Store
from .telegram import Telegram

log = logging.getLogger("paragraph_bot")

TICK_SECONDS = 20
PHONE_RE = re.compile(r"^\+?\d{10,15}$")


async def tick_account(pool: RpcPool, store: Store, notifier: MultiNotifier, owner_id: str) -> None:
    acc = store.get_account(owner_id)
    store.update_account(owner_id, last_checked=time.time())
    if not acc["notify"]:
        return
    try:
        rpc = await pool.get(owner_id)
    except AuthExpired:
        if not acc["auth_warned"]:
            store.update_account(owner_id, auth_warned=1)
            await notifier.send(owner_id, "🔑 Сессия истекла. Выполните /login заново.")
        return

    messages: list[str] = []
    for coro in (
        checks.check_balance(rpc, store, owner_id, acc["low_balance_kopecks"]),
        checks.check_transactions(rpc, store, owner_id),
        checks.check_menu(rpc, store, owner_id),
    ):
        try:
            messages += await coro
        except AuthExpired:
            await pool.drop(owner_id)
            break
        except Exception:
            log.exception("проверка для %s упала", owner_id)
    if not messages:
        return
    recipients = [owner_id, *store.list_viewers(owner_id)]
    for text in messages:
        for chat_id in recipients:
            try:
                await notifier.send(chat_id, text)
            except Exception:
                log.exception("не отправил уведомление в чат %s", chat_id)


async def run(once: bool) -> None:
    cfg = config.load()
    store = Store(cfg.database_url)
    tg = Telegram(cfg.telegram_token)
    max_bot = None
    if cfg.max_bot_token:
        from maxapi import Bot as MaxBot
        from maxapi.enums.parse_mode import TextFormat

        max_bot = MaxBot(token=cfg.max_bot_token, format=TextFormat.HTML)
    notifier = MultiNotifier(tg, max_bot)
    pool = RpcPool(cfg, store)
    log.info("фоновые проверки запущены (MAX %s)", "включён" if max_bot else "выключен")
    try:
        while True:
            now = time.time()
            for acc in store.all_accounts():
                if once or now - acc["last_checked"] >= acc["poll_seconds"]:
                    await tick_account(pool, store, notifier, acc["owner_id"])
            if once:
                return
            await asyncio.sleep(TICK_SECONDS)
    finally:
        await pool.close_all()
        await tg.close()
        if max_bot:
            await max_bot.close_session()


async def probe() -> None:
    """Печатает ФОРМУ ответов для первого найденного аккаунта (ключи и типы, без
    значений) — чтобы проверять разбор полей после изменений на сервере."""
    cfg = config.load()
    store = Store(cfg.database_url)
    accounts = store.all_accounts()
    if not accounts:
        sys.exit("нет ни одного вошедшего аккаунта — сперва /login в Telegram")
    rpc = RpcClient(cfg.rpc_url, accounts[0]["token"])
    await rpc.connect()

    def shape(x, depth=0):
        if isinstance(x, dict):
            return {k: shape(v, depth + 1) for k, v in x.items()} if depth < 4 else "{…}"
        if isinstance(x, list):
            return [shape(x[0], depth + 1)] if x else []
        return type(x).__name__

    try:
        for method, params in (
            ("balance", {}),
            ("get_home", {}),
            ("transactions", {"sort_type": "desc", "count": 2}),
        ):
            print(f"## {method}\n{json.dumps(shape(await rpc.call(method, params)), ensure_ascii=False, indent=1)}")
    finally:
        await rpc.close()


async def tg_test() -> None:
    cfg = config.load()
    if not cfg.admin_chat_id:
        sys.exit("TELEGRAM_CHAT_ID не задан в .env")
    tg = Telegram(cfg.telegram_token)
    await tg.send(cfg.admin_chat_id, "✅ paragraph-bot: связь с Telegram работает")
    await tg.close()


async def listen() -> None:
    """Слушает команды/нажатия кнопок от ЛЮБОГО telegram-чата и ведёт диалог входа
    (/login: телефон → пароль). Пароль нигде не хранится и не логируется: только
    используется один раз для вызова signin, затем сразу забыт; само сообщение с
    паролем бот удаляет из чата."""
    cfg = config.load()
    store = Store(cfg.database_url)
    tg = Telegram(cfg.telegram_token)
    pool = RpcPool(cfg, store)

    async def do_login(chat_id: str, phone: str, password: str) -> None:
        store.clear_session(chat_id)
        client = RpcClient(cfg.rpc_url, None)
        try:
            await client.connect_anonymous()
            res = await client.signin(phone, password)
        except (RpcError, OSError, asyncio.TimeoutError):
            log.warning("неудачный вход для чата %s", chat_id)  # без телефона/пароля в логе
            await tg.send(chat_id, "❌ Не удалось войти: проверьте телефон и пароль. /login — попробовать снова.")
            await client.close()
            return
        linked_to = await finish_login(store, client, chat_id, res["token"])
        await client.close()
        if linked_to:
            await tg.send(
                chat_id,
                "✅ Этот логин уже привязан к другому чату (Telegram/MAX) — объединил доступ, "
                "настройки и уведомления теперь общие.",
                reply_markup=commands.MAIN_KEYBOARD,
            )
        else:
            store.update_account(chat_id, auth_warned=0)
            await tg.send(chat_id, "✅ Вход выполнен! Теперь доступны все команды.", reply_markup=commands.MAIN_KEYBOARD)

    async def send_settings(chat_id: str) -> None:
        owner_id = store.resolve_owner(chat_id)
        if owner_id is None:
            await tg.send(chat_id, "Сначала войдите: /login, или привяжитесь через /link КОД.")
            return
        acc = store.get_account(owner_id)
        text = (
            "<b>Настройки</b>\n"
            f"Порог низкого баланса: {checks.rub(acc['low_balance_kopecks'])}\n"
            f"Интервал проверки: {acc['poll_seconds'] // 60} мин\n"
            f"Уведомления: {'🟢 включены' if acc['notify'] else '🔴 выключены'}"
        )
        await tg.send(chat_id, text, reply_markup=commands.settings_keyboard(acc))

    async def handle_set(chat_id: str, args: list[str]) -> None:
        owner_id = store.resolve_owner(chat_id)
        if owner_id is None or len(args) < 2:
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
        await send_settings(chat_id)

    async def handle(chat_id: str, cmd: str, args: list[str]) -> None:
        cmd = cmd.lower()

        if cmd in ("help", "start"):
            await tg.send(chat_id, commands.HELP, reply_markup=commands.MAIN_KEYBOARD)
            return
        if cmd == "login":
            store.set_session(chat_id, "login_phone")
            await tg.send(chat_id, "Отправьте номер телефона для входа (например +79991234567). /cancel — отменить.")
            return
        if cmd == "settings":
            await send_settings(chat_id)
            return
        if cmd == "set":
            await handle_set(chat_id, args)
            return
        if cmd == "share":
            owner_id = store.resolve_owner(chat_id)
            if owner_id is None:
                await tg.send(chat_id, "Сначала войдите в свой аккаунт: /login")
                return
            code = store.create_share_code(owner_id)
            await tg.send(
                chat_id,
                f"Код для привязки: <code>{code}</code>\nДействует 15 минут. "
                f"Другой человек должен написать боту:\n<code>/link {code}</code>",
            )
            return
        if cmd == "link":
            if not args:
                await tg.send(chat_id, "Использование: /link КОД")
                return
            owner = store.redeem_share_code(args[0], chat_id)
            if owner is None:
                await tg.send(chat_id, "Код неверный или истёк. Попросите прислать новый через /share.")
                return
            await tg.send(
                chat_id,
                "✅ Доступ привязан — теперь видите этот аккаунт и будете получать уведомления.",
                reply_markup=commands.MAIN_KEYBOARD,
            )
            return
        if cmd == "unlink":
            store.remove_share(chat_id)
            await tg.send(chat_id, "Отвязано.", reply_markup=commands.MAIN_KEYBOARD)
            return
        if cmd == "viewers":
            owner_id = store.resolve_owner(chat_id)
            if owner_id is None:
                await tg.send(chat_id, "Сначала войдите: /login")
                return
            viewers = [v for v in store.list_viewers(owner_id) if v != chat_id]
            text = "Подключены: " + ", ".join(viewers) if viewers else "Пока никто не подключён. /share — дать доступ."
            await tg.send(chat_id, text)
            return

        owner_id = store.resolve_owner(chat_id)
        if owner_id is None:
            await tg.send(
                chat_id,
                "У вас нет привязанного аккаунта.\n/login — войти самому\n"
                "/link КОД — привязаться к чужому (например, мужа/жены)",
                reply_markup=commands.MAIN_KEYBOARD,
            )
            return
        dispatch_handler = commands.DISPATCH.get(cmd)
        if not dispatch_handler:
            await tg.send(chat_id, "Неизвестная команда. /help — список.", reply_markup=commands.MAIN_KEYBOARD)
            return
        try:
            rpc = await pool.get(owner_id)
            reply = await dispatch_handler(rpc, store, owner_id, args)
            if cmd == "status" and owner_id != chat_id:
                reply += "\n\n🔗 Это общий доступ: вы смотрите чужой аккаунт (владелец вошёл в другом чате/платформе)."
        except AuthExpired:
            reply = (
                "🔑 Сессия истекла. Выполните /login"
                if owner_id == chat_id
                else "🔑 Сессия владельца истекла. Попросите его выполнить /login заново."
            )
        except Exception:
            log.exception("команда /%s упала", cmd)
            reply = "⚠️ Ошибка при выполнении команды"
        keyboard = (
            commands.menu_keyboard(commands.week_from_args(args)) if cmd == "menu" else commands.MAIN_KEYBOARD
        )
        await tg.send(chat_id, reply, reply_markup=keyboard)

    offset: int | None = None
    saved = store.get("tg_offset")
    if saved:
        offset = int(saved) + 1

    log.info("слушаю команды Telegram")
    try:
        while True:
            try:
                updates = await tg.get_updates(offset)
            except Exception:
                log.exception("getUpdates упал, жду 5с")
                await asyncio.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                store.set("tg_offset", str(upd["update_id"]))

                cq = upd.get("callback_query")
                if cq is not None:
                    chat_id = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    await tg.answer_callback(cq["id"])
                    if not chat_id:
                        continue
                    parts = (cq.get("data") or "").split()
                    if parts:
                        await handle(chat_id, parts[0], parts[1:])
                    continue

                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                message_id = msg.get("message_id")

                session = store.get_session(chat_id)
                if session is not None:
                    state, data = session
                    if text == "/cancel":
                        store.clear_session(chat_id)
                        await tg.send(chat_id, "Отменено.", reply_markup=commands.MAIN_KEYBOARD)
                        continue
                    if state == "login_phone":
                        phone = text.replace(" ", "")
                        if not PHONE_RE.match(phone):
                            await tg.send(chat_id, "Похоже, это не номер телефона. Пример: +79991234567. /cancel — отменить.")
                            continue
                        store.set_session(chat_id, "login_password", phone)
                        await tg.send(chat_id, "Теперь отправьте пароль. Сообщение будет сразу удалено из чата.")
                        continue
                    if state == "login_password":
                        if message_id:
                            await tg.delete_message(chat_id, message_id)
                        await do_login(chat_id, data, text)
                        continue

                if not text.startswith("/"):
                    continue  # не команда и нет активной сессии — молча игнорируем
                parts = text[1:].split()
                await handle(chat_id, parts[0], parts[1:])
    finally:
        await pool.close_all()
        await tg.close()


async def max_listen() -> None:
    """Webhook-сервер MAX (aiohttp, порт 8080) — за Caddy/nginx с HTTPS снаружи."""
    cfg = config.load()
    if not cfg.max_bot_token:
        sys.exit("MAX_BOT_TOKEN не задан в .env")
    if not cfg.max_webhook_url:
        sys.exit("MAX_WEBHOOK_URL не задан в .env (например https://paragraph.gomunkul.online/webhook)")
    from . import max_app

    bot, dp, store, pool = max_app.build()

    @dp.on_started()
    async def _subscribe() -> None:
        from maxapi.enums.update import UpdateType

        try:
            await bot.subscribe_webhook(
                url=cfg.max_webhook_url,
                update_types=[
                    UpdateType.MESSAGE_CREATED,
                    UpdateType.MESSAGE_CALLBACK,
                    UpdateType.BOT_STARTED,
                ],
                secret=cfg.max_webhook_secret,
            )
            log.info("MAX webhook подписан: %s", cfg.max_webhook_url)
        except Exception:
            log.exception("не удалось подписаться на MAX webhook")

    log.info("слушаю команды MAX (webhook)")
    try:
        # 0.0.0.0 внутри контейнера — наружу доступ всё равно ограничен Docker
        # (docker-compose публикует порт только на 127.0.0.1 хоста) и Caddy перед ним
        await dp.handle_webhook(
            bot=bot, host="0.0.0.0", port=8080, path="/webhook", secret=cfg.max_webhook_secret
        )
    finally:
        await pool.close_all()
        await bot.close_session()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    cli_commands = {
        "run": lambda: run(False),
        "once": lambda: run(True),
        "listen": listen,
        "max-listen": max_listen,
        "probe": probe,
        "tg-test": tg_test,
    }
    if cmd not in cli_commands:
        sys.exit(f"неизвестная команда {cmd!r}; доступны: {', '.join(cli_commands)}")
    asyncio.run(cli_commands[cmd]())


if __name__ == "__main__":
    main()
