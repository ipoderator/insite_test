# Архитектура

Бот заявок: Flask (WSGI) + SQLite + Telegram Bot API по webhook. Рантайм-зависимости — только `flask` (≥ 2.2) и `requests`. Термины — `CONTEXT.md`, решения — `docs/adr/`.

## Поток данных

1. Telegram → `POST /tg/webhook` (`leadbot/web.py`): неверный секрет → 403; иначе всегда 200, даже при ошибке обработки.
2. `web.py` → `update_handler` (по умолчанию `Engine(...).handle_update`); `mark_update_processed(update_id)` отсекает повторы.
3. `Engine` (`leadbot/engine.py`) грузит сессию из `Storage`, ведёт по графу `scenario.py`, тексты берёт из `TextStore`, ответы шлёт через `TelegramClient`, сессию сохраняет.
4. Сводка → «Отправить» → `Storage.create_lead` → `leads.deliver_lead` → чат владельца (`owner_chat_id`); результат доставки пишется в заявку.
5. Панель `/admin` (`leadbot/admin/`) читает/правит те же таблицы: заявки, тексты (`texts`), контакты, примеры, получатель, политика. «Отправить повторно» = `leads.deliver_lead`.
6. Получатель привязывается командой `/owner КОД` (код — настройка `owner_bind_code`, ротируется в панели).

## Границы модулей

| Модуль | Владеет | Не знает |
|---|---|---|
| `config.py` | окружение, `.env` (`Config`, `ConfigError`, `load_dotenv` не перезаписывает заданное) | всего остального |
| `storage.py` | единственное место с SQL: схема, WAL, транзакции, seed, JSON-сериализация | Telegram, сценарий |
| `texts.py` | каталог `TEXTS` (дефолты), `TextStore.get(key, **vars)` = override из базы или дефолт | SQL |
| `scenario.py` | граф диалога как данные: `NODES`, `BRANCHES`, `HUB_VARIANTS`, `next_node`, `question_path` | рендер, Telegram |
| `validators.py` | чистые функции, `(ok, значение, ключ_ошибки)` | всё |
| `telegram.py` | тонкий клиент Bot API; токен не попадает в логи; «message is not modified» проглатывается | сценарий |
| `engine.py` | конечный автомат: разбор апдейтов, клавиатуры, путь, сводка; не бросает наружу | SQL |
| `leads.py` | сводка, текст заявки владельцу, доставка | состояние диалога |
| `web.py` / `admin/` | маршруты, вход (CSRF, блокировка по IP), формы | логика диалога |
| `wsgi.py` | `.env` рядом с собой → `Config` → `create_app`; относительный `DATABASE_PATH` — от корня проекта | |

## Данные

- Таблицы: settings, texts, sessions, processed_updates, leads, example_categories, example_items, login_failures.
- Сессия (JSON): `accepted_privacy, flow, branch, node, stack, answers, pending_inputs, edit_mode, message_id, submitted, context`. Ключи стабильны; новые поля — только со значением по умолчанию при чтении.
- Ответы: ключ `"<branch>/<node>"`; одинаковые вопросы разных веток — один `Node`. `ns_what` всегда под `"new/ns_what"`, `next_node` для него вызывать с `branch="new"` (и при «Исправить» тоже). Финал — `"final/<fin_*>"`.
- Формы ответов: single `{"option","fields"}`, multi `{"selected","fields"}`, ввод `{"text"}`, хаб `{"fields","done"}`.
- Заявка: `id, kind (new|promo|redesign|examples), chat_id, username, name, phone, items, files, meta, status (по умолчанию "new"), delivered, delivery_error, ...`. `items` — `{"title","answer"}` в порядке пути; `files` — `{"file_id","file_name","mime","size","kind"}`.
- Настройки: `contact_telegram|phone|email`, `privacy_url` (пусто → демо `/privacy`), `privacy_page_text`, `owner_bind_code`, `owner_chat_id`, `seeded_examples`, `admin_session_epoch`.
- Время в базе — UTC ISO-8601; показ — Europe/Moscow.

## Соглашения

- Клиентский и заказчицкий текст — русский; код, идентификаторы, комментарии — английский.
- Любой текст бота — через `TextStore` по ключу `<узел>.<часть>` (`pay.question`, `common.back`, `err.phone`); строк для клиента в движке нет.
- Всё, что уходит с `parse_mode=HTML`, проходит `html.escape`, кроме разметки самого кода (`<b>`, `<a href>` с экранированным URL).
- `callback_data`: `"<node>|<action>|<arg>"`, ≤ 64 байт; действия `pick/toggle/done/back/cat/skip/submit/edit/fix/tosum`.
- Узел со своей кнопкой возврата (`sec_*`, `ex_other`, `ex_card`) общую «Назад» не получает.
- Кнопки Telegram ≤ 60 символов; исключения из PDF — `popup.opt.texts` (63), `rd_what.opt.features` (70): панель разрешает max(60, длина дефолта).
- Секреты не в коде, тестах, логах; URL Bot API с токеном не логируется.
- Код совместим с Flask 2.2: API новее не использовать. `MAX_CONTENT_LENGTH` = 1 МБ на всё приложение, включая webhook.
- IP клиента для блокировки входа: `X-Real-IP` учитывается, только если `REMOTE_ADDR` локальный/частный; Cookie Secure — по `request.is_secure` или `X-Forwarded-Proto=https`.

## Швы для тестов

1. `Engine.handle_update` + `FakeTelegram` (`tests/fakes.py`) + `Storage` на временном файле; сценарии — через `ChatSim` (`tests/sim.py`: `start/click/send/send_contact/send_document`, `last_text`, `buttons`).
2. `create_app(config, storage, tg).test_client()` — панель и webhook.
3. Чистые функции `validators` и `leads.format_*`.

## Скрипты

- `scripts/run_local.py` — консольный чат поверх `Engine` + `FakeTelegram` (`--db`).
- `scripts/deploy_pythonanywhere.py` — деплой и webhook через API PA (`--env`, `--python`, `--dry-run`); код на хостинге в `/home/<user>/leadbot-app`; серверные `TELEGRAM_WEBHOOK_SECRET`/`SECRET_KEY` генерируются и живут только в серверном `.env`.
- `scripts/e2e_hosted.py` — проверка на хостинге (`--chat-id` обязателен, иначе exit 2; `--env`, `--base-url`); восстанавливает тестовый `pr_current.title`, тестовую заявку помечает «Обработана».
- `scripts/common.py` — общие помощники (`PythonAnywhere`, `Redactor`, `read_env_file`, `check_health`, `ScriptError`).
