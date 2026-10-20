# Границы и правила проекта

Каждый исполнитель читает этот файл первым. Имена ниже — контракт: их не переименовывают и не дублируют.

## Правила проекта

- Python ≥ 3.10. Рантайм-зависимости — только `flask` (≥ 2.2) и `requests`; всё прочее — стандартная библиотека (`sqlite3`, `zoneinfo`, `secrets`, `hmac`, `json`, `re`, `logging`). Своего загрузчика `.env` хватит, `python-dotenv` не нужен. Для разработки — `pytest`.
- Пакет `leadbot/` в корне репозитория. Тесты — в `tests/`. Скрипты — в `scripts/`. Документация — в `docs/`.
- Локальное окружение: `uv venv .venv && uv pip install -r requirements-dev.txt`. Проверка одной командой: `.venv/bin/python -m pytest -q`.
- Никаких секретов в коде, тестах и логах. Токен бота не логируется: URL Bot API с токеном никогда не пишется в лог.
- Весь текст, который видит клиент или заказчик, — на русском. Код, идентификаторы, комментарии — на английском.
- Каждый текст, который отправляет бот, берётся из `TextStore` по ключу (дефолты — в `leadbot/texts.py`). Строк для клиента в коде движка нет.
- Всё, что уходит в Telegram с `parse_mode=HTML`, проходит через `html.escape`, кроме разметки, которую ставит сам код (`<b>` заголовка, `<a href>` ссылок с экранированным URL).
- Время в базе — UTC ISO-8601. Показ — Europe/Moscow.

## Границы, решённые в спецификации

### `leadbot/config.py`
- Владеет: чтением окружения и `.env`.
- Отдаёт: `@dataclass(frozen=True) class Config: bot_token: str; webhook_secret: str; admin_password: str; secret_key: str; database_path: str; public_base_url: str` · `Config.from_env(env: Mapping[str,str] | None = None) -> Config` (бросает `ConfigError` с понятным текстом, если чего-то нет или `admin_password` короче 10 символов) · `load_dotenv(path: str) -> None` (не перезаписывает уже заданные переменные).

### `leadbot/storage.py` — единственный модуль, который знает SQL
- Владеет: схемой SQLite и миграцией при старте (`CREATE TABLE IF NOT EXISTS`), WAL-режимом, транзакциями.
- Отдаёт: `class Storage(path: str)`. Методы:
  - `init_schema() -> None`; `seed_defaults() -> None` — сферы и демо-примеры из PDF, контакты из PDF, код привязки владельца; идемпотентно, правки заказчика не перетирает.
  - Настройки: `get_setting(key, default=None) -> str|None`, `set_setting(key, value) -> None`, `all_settings() -> dict`.
  - Тексты: `get_text_override(key) -> str|None`, `set_text_override(key, value) -> None`, `delete_text_override(key) -> None`, `all_text_overrides() -> dict[str,str]`.
  - Сессии: `load_session(chat_id: int) -> dict|None`, `save_session(chat_id: int, data: dict) -> None`, `delete_session(chat_id) -> None`.
  - Идемпотентность: `mark_update_processed(update_id: int) -> bool` (False — апдейт уже был); старые записи чистятся сами.
  - Заявки: `create_lead(kind: str, chat_id: int, username: str|None, name: str|None, phone: str|None, items: list[dict], files: list[dict], meta: dict) -> int`; `get_lead(id) -> dict|None`; `list_leads(kind=None, status=None, limit=200) -> list[dict]`; `set_lead_delivery(id, delivered: bool, error: str|None) -> None`; `set_lead_status(id, status: str) -> None`. `items` — список `{"title": str, "answer": str}` в порядке пути; `files` — `{"file_id","file_name","mime","size","kind": "document"|"photo"}`.
  - Примеры работ: `list_categories(active_only=False) -> list[dict]` (`id, slug, button, topic, sort, active`), `get_category(id|slug)`, `save_category(dict) -> int`, `delete_category(id)`, `list_items(category_id) -> list[dict]` (`id, category_id, title, url, note, is_demo, sort`), `save_item(dict) -> int`, `delete_item(id)`.
  - Вход в панель: `record_login_failure(ip) -> None`, `recent_login_failures(ip, minutes=15) -> int`, `clear_login_failures(ip) -> None`.
- Скрывает: SQL, имена таблиц, JSON-сериализацию.

### `leadbot/texts.py`
- Владеет: каталогом всех текстов бота.
- Отдаёт: `TEXTS: dict[str, TextDef]`, где `@dataclass(frozen=True) class TextDef: key: str; group: str; label: str; default: str; kind: Literal["message","button","info","error","title"]` · `GROUPS: list[tuple[str,str]]` (id группы, русское название, порядок вывода в панели) · `class TextStore(storage)`: `get(key: str, **vars) -> str` (override или default; `{name}`-подстановка через `str.replace`; неизвестный ключ — `KeyError`), `is_overridden(key) -> bool`.
- Ключи именуются `<узел>.<часть>`: `pay.question`, `pay.hint`, `pay.opt.yes`, `pay.title` (краткий заголовок для сводки и «Исправить»), `common.back`, `common.done`, `err.phone` и т. д.

### `leadbot/scenario.py` — граф диалога как данные
- Владеет: узлами, вариантами, ветками, переходами.
- Отдаёт: `@dataclass(frozen=True) class Option: id: str; text_key: str; next: str|None = None; input: InputSpec|None = None; exclusive: bool = False; final_jump: bool = False` · `@dataclass(frozen=True) class InputSpec: kind: Literal["text","url","urls","file"]; prompt_key: str; optional: bool = False` · `@dataclass(frozen=True) class Node: id: str; kind: Literal["single","multi","text","url","file","hub","info","phone","name"]; question_key: str; title_key: str|None; options: tuple[Option,...] = (); hint_key: str|None = None; footer_key: str|None = None; done_key: str|None = None; branching: bool = False` · `NODES: dict[str, Node]` · `BRANCHES: dict[str, tuple[str,...]]` — упорядоченные последовательности узлов для `goods`, `services`, `both`, `other`, `promo`, `redesign`, `final` · `HUB_VARIANTS: dict[str, tuple[str,...]]` (`goods`, `services`) — категории хаба и их пункты · `next_node(branch: str, node_id: str, answers: dict) -> str|None` (учитывает условие pr_current → pr_helpers / FINAL).
- Правило: одинаковые вопросы разных веток — один `Node`; ответ хранится по ключу `f"{branch}/{node_id}"`.

### `leadbot/validators.py`
- Отдаёт чистые функции, каждая возвращает `Result = tuple[bool, str|None, str|None]`: (ok, нормализованное значение, ключ ошибки для `TextStore`): `validate_name(s)`, `validate_phone(s)`, `validate_url(s)`, `validate_urls(s)` (значение — ссылки через перенос строки), `validate_free_text(s, min_len=2, max_len=1000)`, `validate_file(file_name: str|None, mime: str|None, size: int|None, kind: str)`.

### `leadbot/telegram.py`
- Отдаёт: `class TelegramError(Exception)` (с `.description`) · `class TelegramClient(token: str, timeout=15)` с методами `send_message(chat_id, text, reply_markup=None, disable_preview=True) -> dict` (возвращает Message), `edit_message_text(chat_id, message_id, text, reply_markup=None) -> None`, `edit_reply_markup(chat_id, message_id, reply_markup=None) -> None`, `answer_callback(callback_id, text=None, alert=False) -> None`, `send_document(chat_id, file_id, caption=None)`, `send_photo(chat_id, file_id, caption=None)`, `get_file_url(file_id) -> str` (только для серверного скачивания в панели, наружу не отдаётся), `set_webhook(url, secret, allowed_updates)`, `get_webhook_info() -> dict`, `set_my_commands(commands)`, `get_me() -> dict`. Ошибка «message is not modified» проглатывается. `parse_mode="HTML"` по умолчанию.
- Шов для тестов: `FakeTelegram` в `tests/fakes.py` с тем же набором методов; записывает вызовы и выдаёт растущие `message_id`.

### `leadbot/engine.py` — конечный автомат
- Отдаёт: `class Engine(storage: Storage, tg: TelegramClient-like, texts: TextStore | None = None)` · `handle_update(update: dict) -> None` — единственная точка входа для апдейтов; не бросает исключений наружу (логирует и отвечает клиенту «Что-то пошло не так, нажмите /start»).
- Состояние сессии (JSON в `Storage`): `{"accepted_privacy": bool, "flow": str|None, "branch": str|None, "node": str|None, "stack": [str], "answers": {key: value}, "pending_inputs": [...], "edit_mode": bool, "message_id": int|None, "submitted": bool, "context": {...}}`. Ключи стабильны; новые поля — только с значением по умолчанию при чтении.
- Callback-data: `"<node_id>|<action>|<option_id>"`, ≤ 64 байт; action ∈ `pick, toggle, done, back, cat, nav, skip, edit, submit, ...`.
- Скрывает: рендер клавиатур, разбор апдейтов, путь и сводку.

### `leadbot/leads.py`
- Отдаёт: `build_items(session: dict, texts: TextStore) -> list[dict]` (вопросы пути в порядке + контакты) · `format_summary(items, texts) -> list[str]` (части ≤ 4096) · `format_lead_for_owner(lead: dict, texts) -> list[str]` · `deliver_lead(lead_id: int, storage, tg, texts) -> bool` (шлёт текст и файлы владельцу, пишет результат доставки).

### `leadbot/web.py`
- Отдаёт: `create_app(config: Config, storage: Storage|None = None, tg=None) -> Flask`. Маршруты: `POST /tg/webhook` (403 без верного секрета; иначе 200 всегда, даже при ошибке обработки) · `GET /health` → `{"ok": true}` · `GET /privacy` (демо-политика) · `/admin/...` (вход, заявки, тексты, инфоблоки, контакты, примеры, получатель, политика, помощь).
- Шаблоны — `leadbot/templates/`, статика — `leadbot/static/` (свой CSS, без CDN).
- Шов для тестов: `create_app` с подставными `storage` и `tg` (`FakeTelegram`), `app.test_client()`.

### `wsgi.py` (корень)
- Загружает `.env` рядом с собой, строит `Config.from_env()`, отдаёт `application = create_app(config)`.

### `scripts/`
- `run_local.py` — локальный запуск без Telegram: консольный симулятор чата поверх `Engine` + `FakeTelegram`, для ручной проверки веток.
- `deploy_pythonanywhere.py` — деплой и webhook (см. spec «Хостинг и деплой»).
- `e2e_hosted.py` — проверка на хостинге.

## Швы для тестов

1. `Engine.handle_update` + `FakeTelegram` + `Storage` на временном файле — все сценарные тесты (через симулятор `tests/sim.py`: `ChatSim.start()`, `.click("подпись")`, `.send("текст")`, `.send_contact(...)`, `.send_document(...)`, `.last_text`, `.buttons`).
2. `create_app(...).test_client()` — панель и webhook.
3. Чистые функции `validators` и `leads.format_*`.

## Команды

| Что | Команда |
|---|---|
| Установка | `uv venv .venv --python 3.11 && uv pip install --python .venv/bin/python -r requirements-dev.txt` (окружение уже создано: flask 3.1, requests 2.34, pytest 9.1) |
| Проверка целиком | `.venv/bin/python -m pytest -q` |
| Один файл | `.venv/bin/python -m pytest -q tests/test_storage.py` |
| Локальный сервер | `.venv/bin/flask --app wsgi run` (нужен `.env` по `.env.example`) |

Совместимость: код должен работать и на Flask 2.2 (на PythonAnywhere может стоять старее) — не использовать API, появившиеся после Flask 2.2.

## Не трогать

`.autopilot/`, `CLAUDE.md`, `.claude/`, `.mcp.json`, `skills-lock.json` — служебные файлы, не часть продукта.

## Что построили завершённые таски

### T01 — фундамент
- `Config`/`ConfigError`/`load_dotenv` как выше; `PUBLIC_BASE_URL` необязателен, хвостовой `/` срезается; секрет webhook проверяется на алфавит Telegram.
- `Storage(path, clock=None)` сам вызывает `init_schema`. Дополнительно: `rotate_owner_bind_code() -> str`, модульная `new_bind_code()`. Таблицы: settings, texts, sessions, processed_updates, leads, example_categories, example_items, login_failures.
- Заявка (dict): `id, kind, chat_id, username, name, phone, items, files, meta, status` (по умолчанию `"new"`), `delivered: bool, delivery_error, delivered_at, created_at, updated_at`. `list_leads` — новые сверху. `save_category`/`save_item`: без `id` — вставка, с `id` — частичное обновление; `slug` и `sort` генерируются, если не заданы.
- Сферы: `active: bool`; примеры: `is_demo: bool`. `topic` — вся фраза после «по» («строительной тематике», «тематике производства / промышленности»), шаблон: `"Вот несколько примеров сайтов по {topic}:"`.
- Настройки после seed: `contact_telegram`, `contact_phone`, `contact_email`, `privacy_url=""` (пусто → демо-страница `/privacy`), `owner_bind_code` (8 символов), флаг `seeded_examples`. Ключ получателя — назвать `owner_chat_id` (задаёт T03).
- Валидаторы, ключи ошибок: `err.name, err.phone, err.url, err.text_short, err.text_long, err.file_type, err.file_size`. `validate_file` возвращает имя файла (`"photo.jpg"` для фото). `validate_phone` принимает голые цифры только в российских формах (8…/7…/9…) — международный номер из контакта Telegram движок дополняет `+`.
- `TelegramClient(token, timeout=15, session=None)`; `TelegramError(description, error_code=None)`.
- `create_app(config, storage=None, tg=None, update_handler=None)`; `get_state(app) -> LeadbotState(config, storage, tg, update_handler)` — T03 подключает `get_state(app).update_handler = engine.handle_update` (или передаёт `update_handler`). `create_app` всегда вызывает `storage.seed_defaults()`.
- `tests/fakes.py`: `FakeTelegram` — `.calls [(method, kwargs)]`, `.calls_of(m)`, `.sent_texts(chat_id)`, `.messages`, `.fail {method: exc}`, `.reset()`; `make_update(update_id, text, chat_id)`.

### T02 — тексты и граф сценария
- Дополнительно к контракту: `Node.intro_key=None` (инфоблок перед вопросом), `Option.extra_inputs=()` и свойство `Option.inputs` (все поля варианта по порядку), `Node.option(id)`. Константы: `FINAL="fin_name"`, `NEW_SITE="new"` (ns_what отвечается под ключом `"new/ns_what"`), `HUB_DONE_MESSAGE_KEY`, `NO_BACK_NODES`, `NEW_SITE_BRANCHES`, `LEAD_KIND_BY_BRANCH`, `HUB_VARIANT_BY_BRANCH`.
- Функции: `selected_option_ids(answer)` (принимает `"id"`, `[ids]`, `{"option"|"selected"}`), `option_branch(node, opt)`, `hub_categories(branch)`, `hub_options(branch)`, `question_path(branch, answers)` (порядок сводки и перестройка пути). `next_node` возвращает None после `fin_time` и для экранов вне веток.
- Хаб: узел `sections` (kind hub). `hub_options` отдаёт кнопки категорий (`Option.next` = узел категории `sec_*`, kind multi) и «Иное» (текстовое поле). Категории закрываются `done_key="common.back_to_sections"`; у каждой есть вариант `add` с текстовым полем, кроме `sec_legal`. В варианте `services` вместо категории «Услуги и товары» — `sec_services` без «Товары (если есть каталог)».
- Поля «→ поле» — `Option.inputs`; `prompt_key` служит и ключом значения поля. `example/yes`: ссылка, затем необязательный комментарий (кнопка «Пропустить»); `example/tz` — файл. В multi поля спрашиваются после «Готово». `exclusive` и `final_jump` — флаги варианта (`final_jump`: pr_current never/hard). `branching=True` у ns_what (id варианта = имя ветки) и pr_current.
- Прочие экраны — тоже узлы: privacy, menu, resume, summary (submit/edit), thanks, ex_sphere (сферы из базы идут перед «other»), ex_card, ex_other, ex_other_done, order, contacts. `pr_link/use` показывается только при известной ссылке на сайт, принимает `{url}`; `fin_phone/share` — кнопка request_contact.
- Ключи текстов: `common.{back,done,continue,skip,back_to_sections,back_to_spheres,to_menu,mark,your_answer,button_inactive,error,already_sent,file_received,multi_hint}`; `summary.{header,kind,files,files_value,viewed_examples,skipped,opt.submit,opt.edit,edit_question,back}`; `lead.{header,date,kind,client,no_username,answers,request,files,file_caption,kind.new|redesign|promo|examples}`; `owner.{bound,bad_code,test}`; `err.*` (7 ключей валидаторов + `empty_multi, no_files, need_buttons, need_text, need_file, need_phone`); `ex_card.{header {topic},demo,empty}`; `contacts.{header,telegram,phone,email} {value}`; `sections.cat_selected {label}{count}`; `cmd.start/menu`. `privacy.text` содержит плейсхолдер `{policy}` — место ссылки на политику.
- Две кнопки из PDF длиннее 60 символов (`popup.opt.texts` 63, `rd_what.opt.features` 70): панель (T04) должна разрешать max(60, длина значения по умолчанию).
- Правила для движка (по ревью T02): (1) у узла, где есть своя кнопка возврата — `done_key="common.back_to_sections"` у `sec_*`, вариант «Назад к списку сфер» у `ex_other`/`ex_card`, — общая «⬅️ Назад» не добавляется: на экране не должно быть двух «Назад». (2) Ответ ns_what всегда лежит под `"new/ns_what"`; `scenario.next_node` для ns_what корректно работает только с `branch="new"` — движок вызывает его с `"new"`, в том числе при исправлении из сводки.

### T03 — движок и заявки
- `Engine(storage, tg, texts=None, public_base_url="")`, `Engine.handle_update(update) -> None`, `engine.new_session() -> dict`. Дополнительные ключи `context`: `q_msg, resume_from, reply_kb, intro_shown, example_category, viewed_examples, thanks_name, last_lead_id`.
- Callback: `"<node>|<action>|<arg>"`, действия `pick/toggle/done/back/cat/skip/submit/edit/fix/tosum`; список «Исправить» — узел `summary` с `edit_mode=True`.
- Формы ответов: single `{"option","fields"}`, multi `{"selected","fields"}`, ввод `{"text"}`, хаб `{"fields","done"}`. Ключи: `"new/ns_what"`, `"final/<fin_*>"`, `"<branch>/<node>"`.
- `leads`: `answer_key, answer_text, hub_answer_text, node_answer_text, session_path, build_items, lead_files, format_summary(items, texts, kind=None, files_count=0), format_lead_for_owner(lead, texts), split_blocks(blocks, limit=4096), deliver_lead(lead_id, storage, tg, texts) -> bool, NO_RECIPIENT_ERROR`.
- `meta` заявки: `{user_id, first_name, last_name, branch, viewed_examples}`; типы `new/promo/redesign/examples`; настройка получателя `owner_chat_id`. `create_app` по умолчанию ставит `update_handler = Engine(storage, tg, public_base_url=config.public_base_url).handle_update`. Панель (T04) для правки текстов создаёт `TextStore(storage)`; для «Отправить повторно» вызывает `leads.deliver_lead`.
- `tests/sim.py`: `ChatSim(storage, tg=None, engine=None, chat_id, username, first_name, chat_type)` — `start/send/send_contact/send_document/send_photo/send_sticker/click/press/replay`, `last_text/current_text/buttons/popups/reply_keyboard`; хелперы `Env, label, plain, walk, answer_default, assert_question, check_html`.

### T04 — веб-панель
- `leadbot.admin.register_admin(app)` вызывается в `create_app`. Blueprint `admin`: `/admin`, `/admin/login`, `POST /admin/logout`, `/admin/leads[?kind&status]`, `/admin/leads/<id>` (+ `POST …/status|resend`, `GET …/files/<n>`), `/admin/texts[?group|q]`, `/admin/info`, `POST /admin/texts/<key>[/reset]`, `/admin/contacts`, `/admin/policy` (+ `POST /reset`), `/admin/examples` (+ `/new`, `/<cid>`, `/toggle`, `/move`, `/delete`, `/<cid>/items/new|<iid>|move|delete`), `/admin/recipient` (+ `POST test|unbind|code`), `/admin/help`. `GET /privacy` (endpoint `privacy_page`).
- Новые настройки: `privacy_page_text` (пусто → демо-текст с плейсхолдерами `[НАЗВАНИЕ КОМПАНИИ…]` и т. п.), `admin_session_epoch` (выход завершает все сессии). Поле формы — `csrf_token`.
- IP клиента для блокировки входа: `X-Real-IP` учитывается, только если `REMOTE_ADDR` локальный или частный (балансировщик PA). Cookie Secure — по `request.is_secure` или `X-Forwarded-Proto=https`. Оба — проверить на хостинге (T05 e2e). `MAX_CONTENT_LENGTH` = 1 МБ на всё приложение, включая webhook.

### T05 — деплой, проверка, документы
- `scripts/deploy_pythonanywhere.py`: `main(argv, session=None, sleep=time.sleep, out=None) -> int`, флаги `--env`, `--python` (по умолчанию `python311`), `--dry-run`. Код — в `/home/<user>/leadbot-app`, WSGI-файл делает `from wsgi import application`. Сгенерированные `TELEGRAM_WEBHOOK_SECRET`/`SECRET_KEY` живут в серверном `.env` и переиспользуются; в локальный `.env` не пишутся.
- `scripts/e2e_hosted.py`: `main(argv, session, sleep, out) -> int`, `--chat-id` обязателен (без него exit 2), `--env`, `--base-url`. message_id активного вопроса узнаёт пробой (sendMessage без звука + deleteMessage) — владелец не пишет боту во время прогона. Тестовое имя «Тест Автопроверка» (цифры имя не проходит). Помеченный тестовый заголовок `pr_current.title` восстанавливается в `finally`; тестовая заявка отмечается «Обработана».
- `scripts/run_local.py`: `main(argv, input_fn=input, out=None)`, флаг `--db`. `scripts/common.py`: `read_env_file, parse_env_text, format_env_file, Redactor, Console, PythonAnywhere(username, token, host, session, sleep), pa_domain, wsgi_file_path, app_dir, check_pa_settings, server_env, check_health, ScriptError`.
- `docs/REPORT.md` исполнителю писать запрещено средой — черновик в `.autopilot/<dir>/report-draft.md`, переносится в финале.
