from datetime import datetime, timedelta, timezone

import pytest

from leadbot.storage import Storage


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "sub" / "bot.sqlite3")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def st(path, clock):
    return Storage(path, clock=clock)


# --- schema / persistence ---------------------------------------------------

def test_creates_parent_dir_and_schema_is_idempotent(path):
    s = Storage(path)
    s.init_schema()
    s.init_schema()
    s.set_setting("a", "1")
    assert Storage(path).get_setting("a") == "1"


def test_data_survives_reopen(path):
    s = Storage(path)
    s.set_setting("contact_phone", "+7 000")
    s.set_text_override("menu.question", "Привет")
    s.save_session(5, {"node": "ns_what", "answers": {"goods/design": ["a"]}})
    lead_id = s.create_lead("new_site", 5, "user", "Анна", "+79991234567",
                            [{"title": "Q", "answer": "A"}], [], {})
    s.mark_update_processed(77)

    s2 = Storage(path)
    assert s2.get_setting("contact_phone") == "+7 000"
    assert s2.get_text_override("menu.question") == "Привет"
    assert s2.load_session(5)["node"] == "ns_what"
    assert s2.get_lead(lead_id)["name"] == "Анна"
    assert s2.mark_update_processed(77) is False


# --- settings ---------------------------------------------------------------

def test_settings_crud(st):
    assert st.get_setting("x") is None
    assert st.get_setting("x", "dflt") == "dflt"
    st.set_setting("x", "1")
    st.set_setting("x", "2")
    st.set_setting("y", "")
    assert st.get_setting("x") == "2"
    assert st.get_setting("y", "dflt") == ""
    assert {"x": "2", "y": ""}.items() <= st.all_settings().items()


# --- text overrides ---------------------------------------------------------

def test_text_overrides(st):
    assert st.get_text_override("a.b") is None
    st.set_text_override("a.b", "<b>не html</b>")
    st.set_text_override("a.b", "второй")
    st.set_text_override("c.d", "x")
    assert st.get_text_override("a.b") == "второй"
    assert st.all_text_overrides() == {"a.b": "второй", "c.d": "x"}
    st.delete_text_override("a.b")
    st.delete_text_override("missing")
    assert st.get_text_override("a.b") is None
    assert st.all_text_overrides() == {"c.d": "x"}


# --- sessions ---------------------------------------------------------------

def test_sessions(st):
    assert st.load_session(1) is None
    data = {"accepted_privacy": True, "stack": ["menu", "ns_what"],
            "answers": {"goods/design": ["Минимализм"], "final/name": "Анна"}, "message_id": None}
    st.save_session(1, data)
    assert st.load_session(1) == data
    st.save_session(1, {"node": "x"})
    assert st.load_session(1) == {"node": "x"}
    st.save_session(-100123, {"group": True})  # group chats have negative ids
    assert st.load_session(-100123) == {"group": True}
    st.delete_session(1)
    assert st.load_session(1) is None


# --- processed updates ------------------------------------------------------

def test_mark_update_processed(st):
    assert st.mark_update_processed(1) is True
    assert st.mark_update_processed(1) is False
    assert st.mark_update_processed(2) is True


def test_old_processed_updates_are_cleaned(st, clock):
    st.mark_update_processed(1)
    clock.advance(days=8)
    st.mark_update_processed(2)  # triggers cleanup of old records
    assert st.mark_update_processed(1) is True
    assert st.mark_update_processed(2) is False


# --- leads ------------------------------------------------------------------

ITEMS = [{"title": "Что нужно?", "answer": "Создать новый сайт"},
         {"title": "Имя", "answer": "Анна"}]
FILES = [{"file_id": "F1", "file_name": "tz.pdf", "mime": "application/pdf",
          "size": 1000, "kind": "document"}]


def test_create_and_get_lead(st):
    lead_id = st.create_lead("new_site", 42, "anna", "Анна", "+79991234567", ITEMS, FILES,
                             {"examples_sphere": "Автомобили"})
    lead = st.get_lead(lead_id)
    assert lead["id"] == lead_id
    assert lead["kind"] == "new_site"
    assert lead["chat_id"] == 42
    assert lead["username"] == "anna"
    assert lead["phone"] == "+79991234567"
    assert lead["items"] == ITEMS
    assert lead["files"] == FILES
    assert lead["meta"] == {"examples_sphere": "Автомобили"}
    assert lead["status"] == "new"
    assert lead["delivered"] is False
    assert lead["delivery_error"] is None
    assert lead["created_at"] == "2026-10-20T12:00:00+00:00"
    assert st.get_lead(9999) is None


def test_lead_with_nulls(st):
    lead_id = st.create_lead("examples", 1, None, None, None, [], [], {})
    lead = st.get_lead(lead_id)
    assert lead["username"] is None and lead["name"] is None and lead["phone"] is None
    assert lead["items"] == [] and lead["files"] == [] and lead["meta"] == {}


def test_lead_delivery_and_status(st, clock):
    lead_id = st.create_lead("promo", 1, None, "Ли", "+79991234567", ITEMS, [], {})
    st.set_lead_delivery(lead_id, False, "получатель не задан")
    lead = st.get_lead(lead_id)
    assert lead["delivered"] is False and lead["delivery_error"] == "получатель не задан"
    clock.advance(minutes=5)
    st.set_lead_delivery(lead_id, True, None)
    lead = st.get_lead(lead_id)
    assert lead["delivered"] is True and lead["delivery_error"] is None
    assert lead["delivered_at"] == "2026-10-20T12:05:00+00:00"
    st.set_lead_status(lead_id, "done")
    assert st.get_lead(lead_id)["status"] == "done"


def test_list_leads_filters_newest_first(st):
    a = st.create_lead("new_site", 1, None, "A", None, [], [], {})
    b = st.create_lead("promo", 2, None, "B", None, [], [], {})
    c = st.create_lead("new_site", 3, None, "C", None, [], [], {})
    st.set_lead_status(c, "done")
    assert [l["id"] for l in st.list_leads()] == [c, b, a]
    assert [l["id"] for l in st.list_leads(kind="new_site")] == [c, a]
    assert [l["id"] for l in st.list_leads(status="new")] == [b, a]
    assert [l["id"] for l in st.list_leads(kind="new_site", status="new")] == [a]
    assert [l["id"] for l in st.list_leads(limit=2)] == [c, b]
    assert st.list_leads(kind="redesign") == []


# --- categories and items ---------------------------------------------------

def test_category_crud(st):
    cid = st.save_category({"slug": "auto", "button": "🚗 Автомобили",
                            "topic": "автомобильной тематике", "sort": 20})
    cid2 = st.save_category({"slug": "food", "button": "Еда", "topic": "т", "sort": 10,
                             "active": False})
    cat = st.get_category(cid)
    assert cat == {"id": cid, "slug": "auto", "button": "🚗 Автомобили",
                   "topic": "автомобильной тематике", "sort": 20, "active": True}
    assert st.get_category("auto") == cat
    assert st.get_category("nope") is None and st.get_category(999) is None
    assert [c["id"] for c in st.list_categories()] == [cid2, cid]
    assert [c["id"] for c in st.list_categories(active_only=True)] == [cid]

    assert st.save_category({"id": cid, "button": "Авто", "active": False}) == cid
    cat = st.get_category(cid)
    assert cat["button"] == "Авто" and cat["active"] is False and cat["slug"] == "auto"


def test_category_without_slug_gets_one(st):
    cid = st.save_category({"button": "Новая", "topic": "новой тематике"})
    cat = st.get_category(cid)
    assert cat["slug"] and st.get_category(cat["slug"])["id"] == cid
    assert len(cat["slug"].encode()) <= 20
    cid2 = st.save_category({"button": "Ещё", "topic": "x"})
    assert st.get_category(cid2)["sort"] > cat["sort"]


def test_items_crud_and_cascade(st):
    cid = st.save_category({"slug": "a", "button": "A", "topic": "a"})
    other = st.save_category({"slug": "b", "button": "B", "topic": "b"})
    i1 = st.save_item({"category_id": cid, "title": "Сайт 2", "sort": 2})
    i2 = st.save_item({"category_id": cid, "title": "Сайт 1", "url": "https://x.ru",
                       "note": "n", "is_demo": True, "sort": 1})
    st.save_item({"category_id": other, "title": "Чужой"})
    items = st.list_items(cid)
    assert [i["id"] for i in items] == [i2, i1]
    assert items[0] == {"id": i2, "category_id": cid, "title": "Сайт 1", "url": "https://x.ru",
                        "note": "n", "is_demo": True, "sort": 1}
    assert items[1]["url"] == "" and items[1]["is_demo"] is False

    st.save_item({"id": i1, "title": "Переименован", "url": "https://y.ru"})
    assert st.list_items(cid)[1]["title"] == "Переименован"
    st.delete_item(i2)
    assert [i["id"] for i in st.list_items(cid)] == [i1]

    st.delete_category(cid)
    assert st.get_category(cid) is None
    assert st.list_items(cid) == []
    assert len(st.list_items(other)) == 1


# --- seed -------------------------------------------------------------------

SEED_TOPICS = {
    "Строительство / благоустройство": "строительной тематике",
    "Ремонтные работы": "ремонтной тематике",
    "Производство / промышленность": "тематике производства / промышленности",
    "Автомобили": "автомобильной тематике",
    "Специалисты / услуги (бьюти, консультации, юристы и т.д.)": "тематике “специалисты”",
    "Еда / доставка / кулинария": "тематике еды и кулинарии",
    "Спорт / отдых / тренеры": "тематике спорта и отдыха",
}


def test_seed_defaults_contents(st):
    st.seed_defaults()
    cats = st.list_categories()
    assert len(cats) == 7
    for cat, (label, topic) in zip(cats, SEED_TOPICS.items()):
        assert cat["button"].endswith(label)  # an emoji may precede the PDF label
        assert cat["topic"] == topic
        assert cat["active"] is True
    titles = {c["slug"]: [i["title"] for i in st.list_items(c["id"])] for c in cats}
    assert titles[cats[0]["slug"]] == ["Сайт №1 – Дома под ключ", "Сайт №2 – Укладка брусчатки",
                                       "Сайт №3 – Утепление фасадов"]
    assert titles[cats[6]["slug"]] == ["Сайт №1 – Персональный тренер", "Сайт №2 – Фитнес-клуб"]
    all_items = [i for c in cats for i in st.list_items(c["id"])]
    assert len(all_items) == 3 + 3 + 2 + 2 + 3 + 2 + 2
    assert all(i["is_demo"] is True and i["url"] == "" for i in all_items)

    assert st.get_setting("contact_telegram") == "@site_it"
    assert st.get_setting("contact_phone") == "+7 999 155-10-43"
    assert st.get_setting("contact_email") == "site-it@mail.ru"
    assert st.get_setting("privacy_url") == ""
    code = st.get_setting("owner_bind_code")
    assert code and len(code) >= 6


def test_seed_is_idempotent_and_keeps_edits(path):
    s = Storage(path)
    s.seed_defaults()
    code = s.get_setting("owner_bind_code")
    s.set_setting("contact_phone", "+7 000 000-00-00")
    s.set_setting("contact_email", "")
    first = s.list_categories()[0]
    s.save_category({"id": first["id"], "button": "Своя кнопка"})
    s.delete_category(s.list_categories()[-1]["id"])

    s2 = Storage(path)
    s2.seed_defaults()
    s2.seed_defaults()
    assert s2.get_setting("contact_phone") == "+7 000 000-00-00"
    assert s2.get_setting("contact_email") == ""
    assert s2.get_setting("owner_bind_code") == code
    cats = s2.list_categories()
    assert len(cats) == 6  # the deleted sphere does not come back
    assert cats[0]["button"] == "Своя кнопка"
    assert sum(len(s2.list_items(c["id"])) for c in cats) == 17 - 2  # sport sphere (2 demo items) was deleted


def test_bind_codes_are_random(tmp_path):
    a = Storage(str(tmp_path / "a.db"))
    b = Storage(str(tmp_path / "b.db"))
    a.seed_defaults()
    b.seed_defaults()
    assert a.get_setting("owner_bind_code") != b.get_setting("owner_bind_code")


def test_rotate_owner_bind_code(st):
    st.seed_defaults()
    old = st.get_setting("owner_bind_code")
    new = st.rotate_owner_bind_code()
    assert new != old and st.get_setting("owner_bind_code") == new


# --- login failures ---------------------------------------------------------

def test_login_failures_window(st, clock):
    for _ in range(3):
        st.record_login_failure("1.2.3.4")
    st.record_login_failure("5.6.7.8")
    assert st.recent_login_failures("1.2.3.4") == 3
    assert st.recent_login_failures("5.6.7.8") == 1
    assert st.recent_login_failures("9.9.9.9") == 0

    clock.advance(minutes=10)
    st.record_login_failure("1.2.3.4")
    assert st.recent_login_failures("1.2.3.4") == 4
    clock.advance(minutes=6)  # first three are now 16 minutes old
    assert st.recent_login_failures("1.2.3.4") == 1
    assert st.recent_login_failures("1.2.3.4", minutes=30) == 4

    st.clear_login_failures("1.2.3.4")
    assert st.recent_login_failures("1.2.3.4", minutes=60) == 0
    assert st.recent_login_failures("5.6.7.8", minutes=60) == 1


def test_lead_for_session_is_saved_with_the_session_only_once(path):
    s = Storage(path)
    s.save_session(5, {"node": "summary", "message_id": 40, "answers": {"x": 1}})
    closed = {"node": "thanks", "message_id": None, "answers": {}, "submitted": True}
    args = ("new", "user", "Анна", "+79991234567", [{"title": "Q", "answer": "A"}], [], {})
    expected = {"node": "summary", "message_id": 40}
    lead_id = s.create_lead_for_session(5, expected, closed, *args)
    assert s.get_lead(lead_id)["chat_id"] == 5
    assert s.load_session(5) == closed
    assert s.create_lead_for_session(5, expected, {"node": "other"}, *args) is None
    assert len(s.list_leads()) == 1 and s.load_session(5) == closed
    assert s.create_lead_for_session(6, expected, closed, *args) is None  # no session at all
    assert len(s.list_leads()) == 1 and s.load_session(6) is None
