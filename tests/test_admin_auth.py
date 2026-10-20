"""Admin panel: login, lockout, session cookie, logout, CSRF, safe redirects."""
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pytest

from tests.test_admin_support import PASSWORD, Panel, csrf_from, path_of

ADMIN_PAGES = ["/admin", "/admin/", "/admin/leads", "/admin/leads/1", "/admin/texts",
               "/admin/info", "/admin/contacts", "/admin/policy", "/admin/examples",
               "/admin/recipient", "/admin/help"]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def panel(tmp_path):
    return Panel(tmp_path)


@pytest.mark.parametrize("page", ADMIN_PAGES)
def test_pages_redirect_to_login_without_session(panel, page):
    resp = panel.get(page)
    assert resp.status_code == 302
    assert path_of(resp.location).startswith("/admin/login")


def test_post_without_session_changes_nothing(panel):
    resp = panel.client.post("/admin/texts/menu.text", data={"value": "Взлом"})
    assert resp.status_code in (302, 400)
    assert panel.storage.get_text_override("menu.text") is None


def test_login_page_is_russian_password_form(panel):
    html = panel.page("/admin/login")
    assert 'type="password"' in html and "Пароль" in html and "Войти" in html


def test_wrong_password_shows_error_and_gives_no_access(panel):
    resp = panel.login("not-the-password")
    assert resp.status_code == 401
    assert "Неверный пароль" in resp.get_data(as_text=True)
    assert panel.get("/admin/leads").status_code == 302


def test_right_password_opens_panel(panel):
    resp = panel.login()
    assert resp.status_code == 302 and path_of(resp.location) == "/admin"
    assert panel.get("/admin/leads").status_code == 200


def test_five_failures_lock_the_ip_for_fifteen_minutes(tmp_path):
    clock = Clock()
    panel = Panel(tmp_path, clock=clock)
    for _ in range(5):
        assert panel.login("wrong-password-x").status_code == 401
    resp = panel.login(PASSWORD)  # even the right password is refused now
    assert resp.status_code == 429
    assert "заблокирован" in resp.get_data(as_text=True)
    assert panel.get("/admin/leads").status_code == 302
    # another address is not affected
    other = panel.new_client()
    other.environ_base["REMOTE_ADDR"] = "203.0.113.9"
    assert panel.login(client=other).status_code == 302
    # a forged proxy header from the same public address does not help
    forged = panel.new_client()
    forged.environ_base["HTTP_X_REAL_IP"] = "192.0.2.44"
    assert panel.login(client=forged).status_code == 429
    clock.now += timedelta(minutes=16)
    assert panel.login(PASSWORD).status_code == 302
    assert panel.get("/admin/leads").status_code == 200


def test_successful_login_resets_the_failure_counter(tmp_path):
    panel = Panel(tmp_path)
    for _ in range(4):
        panel.login("wrong-password-x")
    assert panel.login().status_code == 302
    other = panel.new_client()  # same address, not logged in
    for _ in range(4):
        assert panel.login("wrong-password-x", client=other).status_code == 401
    assert panel.login(client=other).status_code == 302


def test_behind_local_proxy_the_real_client_ip_is_counted(tmp_path):
    panel = Panel(tmp_path, remote_addr="10.0.0.5")  # the hosting's load balancer
    first = panel.new_client()
    first.environ_base["HTTP_X_REAL_IP"] = "198.51.100.20"
    for _ in range(5):
        panel.login("wrong-password-x", client=first)
    assert panel.login(client=first).status_code == 429
    second = panel.new_client()
    second.environ_base["HTTP_X_REAL_IP"] = "198.51.100.21"
    assert panel.login(client=second).status_code == 302


def _session_cookie(resp) -> str:
    cookies = [h for h in resp.headers.getlist("Set-Cookie") if h.startswith("session=")]
    assert cookies, resp.headers
    return cookies[-1]


def test_session_cookie_flags_over_http(panel):
    cookie = _session_cookie(panel.login())
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered
    assert "secure" not in lowered.replace("samesite", "")
    expires = parsedate_to_datetime(cookie.split("Expires=")[1].split(";")[0])
    left = expires - datetime.now(timezone.utc)
    assert timedelta(hours=11, minutes=55) < left <= timedelta(hours=12, minutes=1)


def test_session_cookie_is_secure_over_https(tmp_path):
    panel = Panel(tmp_path)
    https = panel.new_client()
    https.environ_base["wsgi.url_scheme"] = "https"
    html = https.get("/admin/login", base_url="https://localhost").get_data(as_text=True)
    resp = https.post("/admin/login", base_url="https://localhost",
                      data={"password": PASSWORD, "csrf_token": csrf_from(html)})
    assert resp.status_code == 302
    assert "; secure" in _session_cookie(resp).lower()


def test_session_expires_after_twelve_hours(panel, monkeypatch):
    panel.login()
    assert panel.get("/admin/leads").status_code == 200
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 12 * 3600 + 60)
    assert panel.get("/admin/leads").status_code == 302


def test_twelve_hours_count_from_login_not_from_last_visit(panel, monkeypatch):
    real = time.time()
    panel.login()
    for hours in (4, 8, 11.9):  # an active admin keeps using the panel
        monkeypatch.setattr(time, "time", lambda h=hours: real + h * 3600)
        assert panel.get("/admin/leads").status_code == 200
    monkeypatch.setattr(time, "time", lambda: real + 12 * 3600 + 60)
    assert panel.get("/admin/leads").status_code == 302


def test_logout_ends_the_session_even_for_a_copied_cookie(panel):
    cookie = _session_cookie(panel.login()).split(";")[0]
    copy = panel.new_client(use_cookies=False)
    assert copy.get("/admin/leads", headers={"Cookie": cookie}).status_code == 200
    resp = panel.post("/admin/logout")
    assert resp.status_code == 302 and path_of(resp.location).startswith("/admin/login")
    assert panel.get("/admin/leads").status_code == 302
    assert copy.get("/admin/leads", headers={"Cookie": cookie}).status_code == 302


def test_logout_requires_post(panel):
    panel.login()
    assert panel.get("/admin/logout").status_code == 405
    assert panel.get("/admin/leads").status_code == 200


@pytest.mark.parametrize("next_path,expected", [
    ("/admin/texts", "/admin/texts"),
    ("https://evil.example/admin", "/admin"),
    ("//evil.example/admin", "/admin"),
    ("/\\evil.example", "/admin"),
    ("/privacy", "/admin"),
    ("javascript:alert(1)", "/admin"),
])
def test_login_redirects_only_to_own_admin_pages(panel, next_path, expected):
    resp = panel.login(next_path=next_path)
    assert resp.status_code == 302
    assert path_of(resp.location) == expected
    assert "evil" not in resp.location


def test_redirect_to_login_keeps_the_requested_page(panel):
    resp = panel.get("/admin/texts?group=new")
    follow = path_of(resp.location)
    assert follow.startswith("/admin/login?next=")
    html = panel.page(follow)
    resp = panel.client.post(follow, data={"password": PASSWORD, "csrf_token": csrf_from(html)})
    assert path_of(resp.location) == "/admin/texts?group=new"


def test_login_post_without_csrf_is_rejected(panel):
    panel.get("/admin/login")
    resp = panel.client.post("/admin/login", data={"password": PASSWORD})
    assert resp.status_code == 400
    assert panel.get("/admin/leads").status_code == 302


@pytest.mark.parametrize("path,data", [
    ("/admin/texts/menu.text", {"value": "Новое приветствие"}),
    ("/admin/contacts", {"contact_telegram": "@x", "contact_phone": "", "contact_email": "",
                         "privacy_url": ""}),
    ("/admin/recipient/code", {}),
    ("/admin/logout", {}),
])
def test_every_post_needs_the_csrf_token(panel, path, data):
    panel.login()
    code_before = panel.storage.get_setting("owner_bind_code")
    missing = panel.post(path, data, csrf=False)
    wrong = panel.post(path, {**data, "csrf_token": "forged"})
    assert missing.status_code == 400 and wrong.status_code == 400
    assert panel.storage.get_text_override("menu.text") is None
    assert panel.storage.get_setting("contact_telegram") == "@site_it"
    assert panel.storage.get_setting("owner_bind_code") == code_before
    assert panel.get("/admin/leads").status_code == 200  # still logged in


def test_admin_responses_are_not_cached_or_framed(panel):
    panel.login()
    resp = panel.get("/admin/leads")
    assert "no-store" in resp.headers.get("Cache-Control", "")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in resp.headers.get("Content-Security-Policy", "")


def test_password_is_never_echoed(panel):
    resp = panel.login("wrong-password-x")
    assert "wrong-password-x" not in resp.get_data(as_text=True)
    assert PASSWORD not in panel.page("/admin/login")
