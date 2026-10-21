# paragraph-bot

Мультипользовательский бот (Telegram + [MAX](https://max.ru)) для платформы
школьного питания «Параграф» ([vshkole.net](https://vshkole.net)): баланс,
покупки, пополнения, меню ребёнка — прямо в мессенджере.

## Возможности

- `/balance` — баланс кошелька
- `/kids` — дети и статус карт
- `/menu [next]` — меню на текущую/следующую неделю, по дням, с ценами
- `/transactions [N]` — последние операции, сгруппированные по дням
- `/status` — состояние аккаунта
- `/settings` — порог низкого баланса, интервал опроса, уведомления вкл/выкл
- `/login` — вход (диалог телефон → пароль прямо в чате)
- `/share`, `/link КОД` — дать доступ к своему аккаунту другому человеку
  (например жене) без передачи логина
- `/unlink`, `/viewers` — управление общим доступом
- Фоновые push-уведомления: низкий баланс, новая операция, изменение меню
- Автоматическая привязка одного и того же логина на разных платформах
  (Telegram ↔ MAX) — по внутреннему `_id` профиля vshkole.net, без /share

Вся логика — на inline-кнопках, текстовые команды не обязательны.

## Архитектура

```
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│    Telegram   │    │      MAX      │    │  vshkole.net  │
│   getUpdates  │    │    webhook    │    │   WebSocket   │
└───────┬───────┘    └───────┬───────┘    │      RPC      │
        │                    │            └───────┬───────┘
  listen (CLI)       max-listen (CLI)             │
        │                    │                    │
     ┌──▼────────────────────▼────────────────────▼──┐
     │                      run                      │
     │      (фоновые проверки, каждые 20 секунд)     │
     └───────────────────────┬───────────────────────┘
                             │
                     ┌───────▼───────┐
                     │    Postgres   │
                     └───────────────┘
```

Один аккаунт vshkole.net = один вход (`/login`), хранится в таблице
`accounts`, ключ — `owner_id` (telegram chat_id либо `max:<user_id>`).
Другие чаты получают доступ через `shares` (вручную по коду `/share`/`/link`
или автоматически — при входе тем же логином на другой платформе).

- **`run`** — единственный процесс фоновых проверок, обслуживает ОБЕ
  платформы (рассылает уведомления через `MultiNotifier` по префиксу
  `owner_id`).
- **`listen`** — слушает команды Telegram (`getUpdates`, long polling).
  Должен быть только один процесс на бота — Telegram иначе вернёт 409.
- **`max-listen`** — webhook-сервер MAX (aiohttp, порт 8080), снаружи
  закрыт Caddy/nginx с HTTPS.
- **Postgres** — общее хранилище для всех трёх процессов.

Бизнес-логика (`checks.py`, `commands.py`, `store.py`, `rpc.py`,
`rpc_pool.py`, `linking.py`) платформонезависима. `telegram.py` и
`max_app.py` — только транспорт.

## Протокол vshkole.net

Публичного HTTP API нет. Кабинет `lk.vshkole.net` работает по
WebSocket-RPC `wss://api.vshkole.net/rpc` — кадр `{"id","method","params"}`,
анонимный метод `_tools` отдаёт схему всех методов сервера. См. `rpc.py`.

## Переменные окружения (`.env`, см. `.env.example`)

| Переменная | Назначение |
|---|---|
| `TELEGRAM_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | чат для админ-уведомлений (`tg-test`), не обязателен |
| `MAX_BOT_TOKEN` | токен бота от @MasterBot в MAX, не обязателен (без него MAX-часть выключена) |
| `MAX_WEBHOOK_URL` | публичный URL вебхука, например `https://example.com/webhook` |
| `MAX_WEBHOOK_SECRET` | секрет для заголовка `X-Max-Bot-Api-Secret` |
| `DATABASE_URL` | строка подключения к Postgres |
| `POSTGRES_DB`/`POSTGRES_USER`/`POSTGRES_PASSWORD` | для сервиса `db` в docker-compose |

## Запуск

### Docker (прод)

```bash
cp .env.example .env   # заполнить
docker compose up -d --build
docker compose logs -f
```

Сервисы: `db`, `run`, `listen`, `max-listen` (последний публикует порт
8080 только на `127.0.0.1` — наружу его должен закрывать реверс-прокси
с HTTPS, см. ниже).

### Локально (разработка)

```bash
uv sync
cp .env.example .env   # заполнить, DATABASE_URL на локальный/докеризованный Postgres
uv run python -m paragraph_bot tg-test   # проверка связи с Telegram
uv run python -m paragraph_bot listen    # команды
uv run python -m paragraph_bot once      # один проход фоновых проверок
uv run python -m paragraph_bot probe     # форма ответов vshkole.net (без значений)
```

## MAX: вебхук за реверс-прокси

MAX требует HTTPS с сертификатом от доверенного CA для входящего вебхука
(самоподписанные/HTTP не принимаются). Пример для Caddy:

```
example.com {
    reverse_proxy localhost:8080
}
```

**Важно:** внутри контейнера aiohttp-сервер должен слушать `0.0.0.0`, а
не `127.0.0.1` — иначе проброс порта Docker не достучится (снаружи будет
`Connection reset`, при этом запрос изнутри контейнера ошибочно выглядит
рабочим). Доступ снаружи и так ограничен публикацией порта на
`127.0.0.1` хоста плюс реверс-прокси перед ним.

### Сертификат Минцифры

Исходящие запросы к `platform-api2.max.ru` падают с
`CERTIFICATE_VERIFY_FAILED`, пока в доверенные не добавлен российский
корневой сертификат — уже встроено в `Dockerfile`
(`update-ca-certificates` на этапе сборки).

## Деплой (VPS, git)

```bash
ssh root@<host>
cd /opt/paragraph-bot
git pull
docker compose up -d --build
```

## Безопасность

- Пароль от vshkole.net никогда не сохраняется: используется один раз в
  `signin` при `/login`, сообщение с паролем бот сразу удаляет из чата.
- `.env`, `state.db*` — вне репозитория (см. `.gitignore`).
- Postgres не публикует порт наружу — доступен только контейнерам в
  compose-сети.
- Опасные методы RPC (`card_block`, `card_set_limit`, `emv_set`,
  `account_delete`, `payment_session`, `nko_transfer_*`) ботом никогда не
  вызываются.
