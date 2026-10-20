"""Admin panel: every page is Russian, mobile-ready and uses no external resources."""
import re
from pathlib import Path

import pytest

import leadbot
from tests.test_admin_support import Panel

PAGES = ["/admin", "/admin/leads", "/admin/texts", "/admin/texts?q=сайт", "/admin/info",
         "/admin/contacts", "/admin/policy", "/admin/examples", "/admin/recipient",
         "/admin/help"]
CSS = Path(leadbot.__file__).parent / "static" / "admin.css"
_EXTERNAL = re.compile(r'<(?:link|script|img|iframe)\b[^>]*(?:href|src)="(?:https?:)?//', re.I)


@pytest.fixture
def panel(tmp_path):
    p = Panel(tmp_path)
    p.login()
    lead_id = p.storage.create_lead("new", 1, "u", "Анна", "+79990000000",
                                    [{"title": "Вопрос", "answer": "Ответ"}], [], {})
    p.lead_id = lead_id
    return p


def all_pages(panel):
    cat = panel.storage.list_categories()[0]
    item = panel.storage.list_items(cat["id"])[0]
    return PAGES + [f"/admin/leads/{panel.lead_id}", f"/admin/examples/{cat['id']}",
                    f"/admin/examples/{cat['id']}/delete",
                    f"/admin/examples/{cat['id']}/items/{item['id']}/delete"]


def test_pages_are_russian_responsive_and_self_contained(panel):
    for path in all_pages(panel):
        page = panel.page(path)
        assert '<html lang="ru">' in page, path
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
        assert '/static/admin.css' in page
        assert not _EXTERNAL.search(page), path
        assert "Как пользоваться" in page  # the menu links the help page
        assert "Выйти" in page


def test_login_and_public_pages_are_self_contained(panel):
    for path in ("/admin/login", "/privacy"):
        page = panel.new_client().get(path).get_data(as_text=True)
        assert 'name="viewport"' in page and not _EXTERNAL.search(page)


def test_stylesheet_is_served_and_has_no_external_imports(panel):
    resp = panel.get("/static/admin.css")
    assert resp.status_code == 200
    css = CSS.read_text(encoding="utf-8")
    assert "@import" not in css and not re.search(r"url\(\s*['\"]?(https?:)?//", css)
    assert "@media (max-width" in css  # a small-screen layout exists


def test_help_page_explains_every_section(panel):
    page = panel.page("/admin/help")
    assert "Как пользоваться панелью" in page
    for section in ("Заявки", "Тексты", "Информационные блоки", "Контакты и ссылки",
                    "Примеры работ", "Получатель заявок", "Политика (демо)"):
        assert f"<h2>{section}</h2>" in page


def test_home_page_shows_status(panel):
    page = panel.page("/admin")
    assert "новых заявок" in page and "Получатель заявок не привязан" in page
