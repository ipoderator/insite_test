# Предложение для CLAUDE.md (вставить строки как есть; сам CLAUDE.md не менялся)

## Бот заявок для веб-студии

Telegram-бот (Flask + SQLite, webhook) ведёт клиента по анкете и шлёт заявку владельцу; заказчик правит тексты и примеры в веб-панели `/admin`. Для агента, дорабатывающего код. Термины — `CONTEXT.md`.

### Команды

```
uv venv .venv --python 3.11 && uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/test_storage.py
.venv/bin/python scripts/run_local.py
.venv/bin/flask --app wsgi run --port 5077
.venv/bin/python scripts/deploy_pythonanywhere.py --dry-run
.venv/bin/python scripts/deploy_pythonanywhere.py
.venv/bin/python scripts/e2e_hosted.py --chat-id <id чата владельца>
```

- `run_local.py` — консольный чат без Telegram; `flask run` нужен `.env` по `.env.example`, панель на `/admin`.
- `deploy` без `--dry-run` — реальный деплой; `e2e_hosted.py` шлёт настоящие сообщения (26 проверок).

### Где что

- Архитектура, поток данных, границы модулей, соглашения: `docs/architecture.md`.
- Граф диалога (узлы, ветки, переходы): `leadbot/scenario.py`; поведение шагов: `leadbot/engine.py`.
- Тексты бота: дефолты в `leadbot/texts.py`, правки живут в базе (панель `/admin/texts`).
- Всё SQL: только `leadbot/storage.py`; панель: `leadbot/admin/` (`views.py`, `auth.py`, `content.py`), шаблоны `leadbot/templates/admin/`.
- Сборка приложения и webhook: `leadbot/web.py`, `wsgi.py`; настройки — `leadbot/config.py`.
- Тестовые швы: `tests/sim.py` (`ChatSim`), `tests/fakes.py` (`FakeTelegram`); деплой — `docs/DEPLOY.md`.
- Решения и почему: `docs/adr/`; словарь: `CONTEXT.md`; состояние работ: `.autopilot/PROGRESS.md`.

### Подводные камни

- API PythonAnywhere не принимает `python_version` в PATCH, только при создании: `docs/adr/0006-d01-python-version-tolko-pri-sozdanii.md`.
- Код должен работать на Flask 2.2 — API новее не использовать.
- WAL SQLite на PythonAnywhere проверен.
- `e2e_hosted.py` шлёт реальные сообщения в чат владельца — владелец не пишет боту во время прогона.
- Тексты бота меняются в панели, а не в коде: дефолт в `leadbot/texts.py` не изменит то, что уже переопределено в базе.
- Строк для клиента в `engine.py` нет — только ключи `TextStore`; HTML в Telegram через `html.escape`.
- `ns_what` хранится под `"new/ns_what"`, `next_node` для него — с `branch="new"`.
- `.env` никогда не в git; `.env.example` — только пустой шаблон. Секреты не логировать.

### Окружение

Имена в `.env` (образец — `.env.example`), значения не читать и не писать в чат:

- `TELEGRAM_BOT_TOKEN` — токен бота от @BotFather.
- `TELEGRAM_WEBHOOK_SECRET` — секрет заголовка webhook; чужим запросам 403.
- `ADMIN_PASSWORD` — пароль `/admin`, не короче 10 символов, иначе приложение не стартует.
- `SECRET_KEY` — подпись сессий панели.
- `DATABASE_PATH` — файл SQLite (по умолчанию `data/bot.sqlite3`).
- `PUBLIC_BASE_URL` — адрес сайта без `/` на конце, для ссылки на `/privacy`.
- `PA_USERNAME`, `PA_API_TOKEN`, `PA_HOST` — только для деплоя на PythonAnywhere (логин, API-токен, хост `www`/`eu`).

### Правка без автопилота

Мелкий баг или доводка после сборки — обычная сессия, без прогона.

Сначала прочитай:
1. `.autopilot/PROGRESS.md` — что не доделано, известные баги, о чём договорились с пользователем.
2. Что правили после последней сборки — первая строка таблицы в «История git».
3. Только если правка их касается: почему сделано так — `docs/adr/`; что значат слова — `CONTEXT.md`;
   что именно просили — `manifest.md` в папке сборки (указана в начале `PROGRESS.md`).

После правки:
- коммит — по правилам из «История git»;
- строка в «Правки без автопилота» в `PROGRESS.md`: дата — что — почему;
- починенный пункт из «Не доделано» или «Известные баги» — удали;
- договорились с пользователем о том, что следующая сборка не должна пересматривать, —
  строка в «Договорились», его словами.

### История git

Целиком не читай — бери срез под вопрос:

| Вопрос | Команда |
|---|---|
| что правили после последней сборки | `git log --oneline "$(git log -1 --format=%h --grep='^Autopilot: сборка сдана')"..HEAD -- . ':(exclude).autopilot'` |
| всё, что делали ради требования | `git log --oneline --grep="R02"` |
| история файла | `git log --oneline -10 -- <файл>` |
| когда появился или исчез код | `git log --oneline -S "<фрагмент>"` |
| почему строка такая | `git blame -L <с>,<по> <файл>`, затем `git show <sha>` |
| что внутри коммита | `git show --stat <sha>` |

Срез длиннее ~30 коммитов — отдай его субагенту и возьми выжимку в десять строк.

Коммит: заголовок — что (`fix: даты в одном формате`), тело — почему, трейлер
`--trailer "Requirements: R02"`, если правка касается требования из `manifest.md`.
Заголовок `Autopilot: сборка сдана` пишет только автопилот — по нему находят последнюю сборку.
