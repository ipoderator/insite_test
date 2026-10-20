import re
import unicodedata

import pytest

from leadbot.storage import Storage
from leadbot.texts import GROUPS, TEXTS, TextStore


@pytest.fixture
def storage(tmp_path):
    return Storage(str(tmp_path / "bot.sqlite3"))


# -- TextStore ----------------------------------------------------------------


def test_store_returns_default_when_no_override(storage):
    store = TextStore(storage)
    assert store.get("common.back") == TEXTS["common.back"].default
    assert store.is_overridden("common.back") is False


def test_store_returns_override_from_database(storage):
    storage.set_text_override("menu.text", "Привет! Что нужно?")
    store = TextStore(storage)
    assert store.get("menu.text") == "Привет! Что нужно?"
    assert store.is_overridden("menu.text") is True
    storage.delete_text_override("menu.text")
    assert store.get("menu.text") == TEXTS["menu.text"].default


def test_store_substitutes_name_in_default_and_override(storage):
    store = TextStore(storage)
    assert "Анна" in store.get("thanks.text", name="Анна")
    assert "{name}" not in store.get("thanks.text", name="Анна")
    storage.set_text_override("thanks.text", "Спасибо, {name}! {name}, ждите звонка.")
    assert store.get("thanks.text", name="Олег") == "Спасибо, Олег! Олег, ждите звонка."


def test_store_substitution_is_plain_replace(storage):
    # braces that are not placeholders must survive (no str.format)
    storage.set_text_override("thanks.text", "Спасибо, {name}! {не переменная} {0}")
    assert TextStore(storage).get("thanks.text", name="Ян") == "Спасибо, Ян! {не переменная} {0}"


def test_store_unknown_key_raises_key_error(storage):
    with pytest.raises(KeyError):
        TextStore(storage).get("no.such.key")
    storage.set_text_override("no.such.key", "x")
    with pytest.raises(KeyError):
        TextStore(storage).get("no.such.key")


def test_store_without_storage_gives_defaults():
    assert TextStore(None).get("common.done") == TEXTS["common.done"].default


# -- catalog shape ------------------------------------------------------------

CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def test_every_text_has_group_label_kind_and_default():
    group_ids = [g for g, _ in GROUPS]
    assert len(group_ids) == len(set(group_ids))
    assert all(CYRILLIC.search(title) for _, title in GROUPS)
    assert TEXTS
    for key, text in TEXTS.items():
        assert text.key == key
        assert text.group in group_ids, key
        assert CYRILLIC.search(text.label), key
        assert text.default.strip(), key
        assert text.kind in ("message", "button", "info", "error", "title"), key


def test_panel_groups_from_spec_exist():
    titles = [title for _, title in GROUPS]
    for title in ["Вход и меню", "Новый сайт", "Продвижение", "Доработка", "Контакты клиента",
                  "Примеры", "Связаться", "Сводка", "Ошибки ввода", "Информационные блоки"]:
        assert title in titles
    used = {t.group for t in TEXTS.values()}
    assert used == {g for g, _ in GROUPS}, "every group has texts"


INFO_BLOCKS = ["privacy.text", "menu.text", "pr_what.intro", "ex_card.note", "fin_name.intro",
               "thanks.text", "contacts.text", "rd_promoted.footer"]


def test_info_blocks_are_a_separate_group():
    info_group = dict(GROUPS)
    info_id = next(g for g, title in GROUPS if title == "Информационные блоки")
    assert info_group[info_id] == "Информационные блоки"
    for key in INFO_BLOCKS:
        assert TEXTS[key].kind == "info", key
        assert TEXTS[key].group == info_id, key
    for text in TEXTS.values():
        assert (text.kind == "info") == (text.group == info_id), text.key


ERROR_KEYS = [
    # validators (T01)
    "err.name", "err.phone", "err.url", "err.text_short", "err.text_long", "err.file_type",
    "err.file_size",
    # engine: empty multi-choice, wrong kind of input
    "err.empty_multi", "err.need_buttons", "err.need_text", "err.need_file", "err.need_phone",
    "err.no_files",
]


def test_error_texts_exist_for_every_validator_and_engine_error():
    for key in ERROR_KEYS:
        assert TEXTS[key].kind == "error", key
    assert TEXTS["err.name"].default == (
        "Похоже, это не имя. Напишите, как к вам обращаться — например, Анна.")
    assert TEXTS["err.phone"].default == "Не получилось распознать номер. Пример: +7 999 123-45-67"
    assert TEXTS["err.url"].default == (
        "Это не похоже на ссылку. Пример: https://example.ru или example.ru")
    assert TEXTS["err.empty_multi"].default == "Выберите хотя бы один вариант"
    assert "1000" in TEXTS["err.text_long"].default
    assert "20 МБ" in TEXTS["err.file_size"].default


MECHANICS_KEYS = [
    "common.back", "common.done", "common.continue", "common.skip", "common.back_to_sections",
    "common.back_to_spheres", "common.to_menu", "common.button_inactive", "common.error",
    "common.already_sent", "common.your_answer", "common.file_received",
    "resume.question", "resume.opt.continue", "resume.opt.restart",
    "owner.bound", "owner.bad_code",
    "summary.header", "summary.opt.submit", "summary.opt.edit", "summary.back",
    "summary.edit_question", "summary.kind",
    "lead.header", "lead.kind.new", "lead.kind.redesign", "lead.kind.promo",
    "lead.kind.examples",
    "ex_card.header", "ex_card.demo", "privacy.link",
]


def test_mechanics_texts_exist():
    missing = [k for k in MECHANICS_KEYS if k not in TEXTS]
    assert not missing


def test_mechanics_wording_from_spec():
    d = {k: t.default for k, t in TEXTS.items()}
    assert "Эта кнопка уже неактивна" in d["common.button_inactive"]
    assert "Пожалуйста, выберите вариант кнопкой ниже" in d["err.need_buttons"]
    assert "Что-то пошло не так, нажмите /start" in d["common.error"]
    assert "Заявка уже отправлена" in d["common.already_sent"]
    assert "У вас есть незаконченная анкета. Продолжить или начать заново?" in d["resume.question"]
    assert "Проверьте заявку" in d["summary.header"]
    assert _plain(d["summary.opt.submit"]) == "Отправить заявку"
    assert _plain(d["summary.opt.edit"]) == "Исправить"
    assert _plain(d["summary.back"]) == "К сводке"
    assert d["common.your_answer"].strip() == "— Ваш ответ:"
    assert _plain(d["common.back"]) == "Назад"
    assert _plain(d["common.done"]) == "Готово"
    assert _plain(d["common.back_to_sections"]) == "Назад к разделам"
    assert _plain(d["common.back_to_spheres"]) == "Назад к списку сфер"
    assert _plain(d["common.to_menu"]) == "В главное меню"
    assert d["ex_card.header"].endswith("Вот несколько примеров сайтов по {topic}:")
    assert d["ex_card.demo"] == "(демо)"
    assert [_plain(d[f"lead.kind.{k}"]) for k in ("new", "redesign", "promo", "examples")] == [
        "Новый сайт", "Доработка", "Продвижение", "Запрос примеров"]


# -- privacy wording ----------------------------------------------------------


def test_privacy_block_uses_honest_wording():
    text = TEXTS["privacy.text"].default
    assert "Перед началом:" in text
    assert "третьим лицам" not in text
    assert ("Ваши ответы нужны только для подготовки предложения по вашему запросу. "
            "Мы не продаём и не передаём их для рекламы. Сообщения проходят через Telegram, "
            "а заявка хранится на сервере, где работает бот. Подробнее — ") in text
    assert "{policy}" in text
    assert TEXTS["privacy.link"].default == "Политика конфиденциальности"


def test_final_message_is_softened_and_complete():
    text = TEXTS["thanks.text"].default
    assert "без посредников" not in text
    assert "Ваша заявка передана специалисту" in text
    for part in [
        "Спасибо за заявку, {name}!",
        "Мы обязательно с вами свяжемся в ближайшее время — обсудим всё подробно и "
        "предложим оптимальные варианты под ваш бюджет и цели.",
        "Если появятся дополнительные вопросы — смело пишите сюда, бот всегда на связи.",
    ]:
        assert part in text


# -- every PDF string is among the defaults -----------------------------------


def _plain(s: str) -> str:
    """Drop emoji / pictographs and collapse whitespace (the PDF lost its emoji)."""
    kept = [ch for ch in s if unicodedata.category(ch) not in ("So", "Sk", "Mn", "Cf", "Cs")]
    return " ".join("".join(kept).split())


# Strings written out by hand from scenario.pdf pages 3–28 (scenario-raw.txt).
# Each must equal some default exactly (ignoring emoji).
PDF_EXACT = [
    # p.3 menu
    "Начать", "Здравствуйте! Чем можем помочь?",
    "Заказать новый сайт", "Доработать текущий сайт", "Продвижение сайта",
    "Примеры наших работ", "Связаться с нами",
    # new site
    "Что вы хотите разместить на сайте?", "Товары", "Услуги", "Иное (ввести вручную)",
    "Нужно ли подключать оплату на сайте?", "Только для ИП и юрлиц", "Да", "Нет",
    "Нужен ли расчёт стоимости доставки?", "СДЭК, ПЭК, BoxBerry, Почта РФ и др.",
    "Интеграция с учётом остатков (1С, МойСклад и др.)?",
    "Интеграция с другими сервисами (CRM, онлайн-чаты и пр.)?",
    "Какой функционал нужен?", "Можно выбрать несколько",
    "Подписка на рассылку", "Купоны/промокоды", "Личный кабинет", "Купить в 1 клик", "Поиск",
    "Квиз", "Онлайн-консультант", "Калькулятор", "Выбор города/языка", "Другое (ввести вручную)",
    "Напишите, какой функционал нужен:",
    "У товаров будут вариации?", "Цвет", "Размер", "Вес", "Объем", "Материал", "Иное (введу)",
    "Напишите, какие вариации:",
    "Сколько товаров планируется?", "< 100", "100–1 000", "1 000–10 000", "> 10 000",
    "Как передадите информацию о товарах?", "Заполню Excel", "Уже есть файл", "Нужно парсить",
    "Пока не знаю",
    "Какие разделы сайта вам понадобятся?", "Выберите категории. Затем уточним внутри каждой.",
    "Основные страницы", "Услуги и товары", "Доверие и отзывы", "Портфолио и проекты",
    "Правовая информация", "Иное", "Готово",
    "Основные страницы:", "О компании / Обо мне", "Контакты", "Прайс / цены", "Акции",
    "Доставка и оплата", "FAQ — Часто задаваемые вопросы", "Добавить свой вариант",
    "Услуги и товары:", "Товары (если есть каталог)", "Сертификаты", "Бренды / марки",
    "Вакансии", "Услуги:",
    "Доверие и отзывы:", "Преимущества", "Отзывы", "Партнеры / сотрудничество",
    "Новости / статьи",
    "Портфолио и проекты:", "Наши проекты", "Работы / объекты", "Фотогалерея",
    "Правовая информация:", "Политика конфиденциальности", "Пользовательское соглашение",
    "Договор-оферта",
    "Напишите, какие страницы ещё нужны:", "Спасибо! Переходим к следующему вопросу.",
    "Пожелания по стилистике и дизайну?", "Полный индивидуальный дизайн + логотип",
    "Только логотип, остальное предложите вы", "Есть логотип, нужен дизайн под него",
    "Главное — просто современно и красиво", "Шаблон подойдет, бюджет ограничен",
    "Хотелось бы проконсультироваться",
    "Есть ли пример сайта, который вам нравится?", "Да, есть", "Нет, нужна помощь с примерами",
    "Есть подробное ТЗ", "Напишите, какие есть примеры и в каком формате:",
    # services
    "В какой сфере вы работаете?", "Выберите сферу, в которой оказываете услуги:",
    "Строительство / благоустройство", "Ремонтные работы", "Производство / промышленность",
    "Автомобили", "Специалисты / услуги (бьюти, консультации, юристы и т.д.)",
    "Еда / доставка / кулинария", "Спорт / отдых / тренеры", "Напишите, чем вы занимаетесь:",
    "Какую структуру сайта вы рассматриваете?", "Это влияет на функционал и стоимость.",
    "Одностраничный сайт (лендинг)", "Многостраничный сайт", "Зависит от бюджета",
    "Затрудняюсь, нужна консультация",
    "У вас есть наполнение для сайта?", "Выберите всё, что уже есть или требуется:",
    "Есть все тексты и материалы", "Есть частично, нужна помощь в подборе",
    "Нужны уникальные тексты", "Требуется подбор изображений", "Затрудняюсь, нужно обсуждение",
    "Напишите, какое наполнение и в каком формате есть:",
    "Нужны ли всплывающие окна (pop-up)?",
    "Они могут облегчить восприятие длинных страниц, либо открываться при клике «Подробнее».",
    "Да, нужны окна для заявки на обратный звонок",
    "Да, нужны окна с текстами/ценами об услугах (укажите сколько)",
    "Нужна консультация, не понимаю, что это", "Знаю, что это, но не уверен(а), нужно ли мне",
    "Сколько и какие окна вы хотите разместить?",
    # goods + services
    "Что будет приоритетом?", "Каталог товаров", "Каталог услуг", "Примерно поровну",
    "Не знаю — нужна помощь",
    "Нужны ли разные потоки: «купить» и «заказать услугу»?", "Да, две разные страницы",
    "Нет, всё в одной системе", "Не уверен(а)",
    # promotion
    "Что вы хотите продвигать?",
    "Выберите всё, что актуально — можно несколько вариантов. "
    "Для каждого откроется поле ввода ссылки.",
    "Сайт", "Соцсети", "Профиль в 2ГИС", "Яндекс Бизнес", "Яндекс Карты", "Яндекс Услуги",
    "Введите ссылку на сайт", "Введите ссылки на ваши соцсети",
    "Пока нет сайта и других каналов", "Не знаю, нужна рекомендация",
    "Введите, что именно хотите продвигать",
    "Какие типы продвижения вам интересны?", "Можно выбрать несколько вариантов",
    "Реклама в Яндекс Директ", "Реклама в 2ГИС", "Реклама в Яндекс Бизнес",
    "Реклама в Яндекс Картах", "Реклама в Яндекс Услугах", "Реклама в соцсетях",
    "SEO-продвижение", "Уточните",
    "Сейчас вы уже что-то продвигаете?", "Да, через платную рекламу",
    "Уточните, какую именно (например, Яндекс Директ, ВК, Google)", "Да, через SEO-продвижение",
    "Да, через соцсети", "Да, но не знаю точно, что это", "Опишите, что делаете сейчас",
    "Продвигали раньше, но сейчас нет", "Опишите, как продвигали",
    "Никогда не продвигали, нужна помощь", "Затрудняюсь ответить, нужна консультация",
    "Вам кто-то уже помогал с продвижением?", "Нет, никогда не обращались",
    "Да, помогали сотрудники Яндекс", "Да, работали с маркетологами / SEO-специалистами",
    "Опишите, кто именно",
    "Вас интересует разовая настройка или сопровождение?",
    "Да, и настройка, и регулярное продвижение", "Только разовая настройка",
    "Нужна консультация", "Уточните ваш запрос",
    "География продвижения:", "Один город", "Несколько регионов", "Вся Россия", "Международно",
    "Какие цели у рекламы?", "Заявки", "Звонки", "Продажи", "Увеличение трафика", "Всё вместе",
    "Оставьте ссылку на сайт, чтобы я проанализировала",
    # redesign
    "На какой платформе сделан ваш сайт?", "WordPress", "Tilda", "OpenCart", "Joomla",
    "1С-Битрикс", "Уточните, на какой платформе сайт", "Не знаю, нужна консультация",
    "Что именно вы хотели бы доработать?", "Возможность выбрать несколько вариантов",
    "Улучшить визуал или сделать редизайн", "Исправить верстку, структуру, логику",
    "Исправить баги, ошибки, нерабочие функции",
    "Изменить структуру: добавить/удалить страницы",
    "Добавить новый функционал (калькулятор, квиз, формы, корзина и т.д.)",
    "Нужны рекомендации специалиста", "Перенос сайта на другую платформу",
    "Введите ссылку на ваш сайт для анализа:", "Также можно выбрать:",
    "Не помню ссылку, нужна помощь", "Сайт не открывается, нужна помощь",
    "Готовы рассмотреть полную пересборку сайта?", "Да, можно предложить и такой вариант",
    "Нет, нужны только доработки",
    "Сейчас сайт продвигается?", "Да, реклама в Яндекс Директ", "Да, в Яндекс Бизнес",
    "Да, SEO-продвижение", "Опишите, как продвигаете сайт", "Нет, пока не продвигаем",
    "Учитываем продвижение, чтобы сохранить позиции при доработке.",
    "У вас есть доступы от сайта?", "Да, у меня есть все доступы (домен и хостинг)",
    "Есть только доступ от домена", "Есть только доступ от хостинга",
    "Не помню/потерял доступы, нужна помощь", "Нет никаких доступов",
    "Опишите ситуацию с доступами",
    # final
    "Ваше имя:", "Номер телефона для связи:", "Мы не будем названивать — только по делу!",
    "Удобный мессенджер для связи:", "WhatsApp", "Telegram", "Звонки", "Продолжить",
    "Когда вам удобно связаться?", "Утро (09:00–12:00)", "День (12:00–17:00)",
    "Вечер (после 17:00)", "Без разницы — пишите в любое время",
    "Вернуться в главное меню", "Связаться напрямую",
    # examples
    "В какой сфере хотите посмотреть примеры сайтов?",
    "Каждый сайт делался под конкретные задачи клиента. Можно адаптировать стиль под вашу нишу.",
    "Заказать услугу", "Назад к списку сфер",
    "Введите, что именно вас интересует — и я пришлю примеры, если есть подходящие сайты "
    "в портфолио.",
    "Отлично! Давайте определим, что вам нужно:", "Создать новый сайт", "В главное меню",
    # contacts
    "Заказать сайт", "Доработать сайт",
]

# Strings that are part of a longer default (multi-paragraph blocks, prompts with a target).
PDF_CONTAINED = [
    "Перед началом:",
    "Товары и услуги",
    "Введите ссылку",
    "Настроим рекламу, которая работает!",
    "Продвигаем ваш бизнес через Яндекс.Директ под ключ — от анализа до запуска и ведения "
    "кампаний.",
    "Остался последний шаг!",
    "Чтобы мы могли с вами связаться и обсудить детали — пожалуйста, оставьте свои контактные "
    "данные:",
    "Спасибо за заявку, {name}!",
    "Мы обязательно с вами свяжемся в ближайшее время — обсудим всё подробно и предложим "
    "оптимальные варианты под ваш бюджет и цели.",
    "Если появятся дополнительные вопросы — смело пишите сюда, бот всегда на связи.",
    "Вот несколько примеров сайтов по",
    "Если у вас остались вопросы — пишите или звоните!",
    "Мы на связи и с радостью поможем",
    "Контакты:",
    "Telegram:", "Телефон:", "Email:",
]


def test_every_pdf_string_is_a_default():
    plain = {_plain(t.default) for t in TEXTS.values()}
    missing = [s for s in PDF_EXACT if _plain(s) not in plain]
    assert not missing


def test_every_pdf_block_is_inside_some_default():
    defaults = [_plain(t.default) for t in TEXTS.values()]
    missing = [s for s in PDF_CONTAINED if not any(_plain(s) in d for d in defaults)]
    assert not missing


def test_texts_from_pdf_keep_their_emoji_slot():
    # buttons of the PDF start with an emoji — the defaults pick one by meaning
    for key in ["menu.opt.new", "menu.opt.redesign", "menu.opt.promo", "menu.opt.examples",
                "menu.opt.contacts", "privacy.start"]:
        assert TEXTS[key].default != _plain(TEXTS[key].default), key
