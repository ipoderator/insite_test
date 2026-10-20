import pytest

from leadbot.validators import (
    MAX_FILE_SIZE,
    validate_file,
    validate_free_text,
    validate_name,
    validate_phone,
    validate_url,
    validate_urls,
)

MB = 1024 * 1024


# --- name -------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Анна", "Анна"),
        ("  Анна  Мария ", "Анна Мария"),
        ("Jean-Luc", "Jean-Luc"),
        ("О'Нил", "О'Нил"),
        ("Ли", "Ли"),
        ("Ёжиков Пётр", "Ёжиков Пётр"),
    ],
)
def test_name_valid(raw, expected):
    assert validate_name(raw) == (True, expected, None)


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "А", "Анна1", "https://spam.ru", "anna@mail.ru", "!!!", "А" * 61, None, "--"],
)
def test_name_invalid(raw):
    assert validate_name(raw) == (False, None, "err.name")


def test_name_60_chars_ok():
    assert validate_name("А" * 60)[0] is True


# --- phone ------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+7 999 123-45-67", "+79991234567"),
        ("8 (999) 123-45-67", "+79991234567"),
        ("89991234567", "+79991234567"),
        ("9991234567", "+79991234567"),
        ("79991234567", "+79991234567"),
        ("+380 50 123 4567", "+380501234567"),
        ("+1 (212) 555-0100", "+12125550100"),
        ("+123456789012345", "+123456789012345"),
    ],
)
def test_phone_valid(raw, expected):
    assert validate_phone(raw) == (True, expected, None)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "123",
        "+7 999 123-45-6",      # +7 needs exactly 10 more digits
        "+1234567890123456",    # 16 digits
        "+12345678",            # 8 digits
        "телефон",
        "8999123456a",
        "1234567890",           # bare digits that are not a Russian number
        "++79991234567",
    ],
)
def test_phone_invalid(raw):
    assert validate_phone(raw) == (False, None, "err.phone")


# --- url --------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://example.ru", "https://example.ru"),
        ("http://example.ru/path?q=1#x", "http://example.ru/path?q=1#x"),
        ("example.ru", "https://example.ru"),
        ("  www.example.com/catalog/  ", "https://www.example.com/catalog/"),
        ("сайт.рф", "https://сайт.рф"),
        ("sub.my-site.co.uk/a_b", "https://sub.my-site.co.uk/a_b"),
        ("HTTPS://Example.ru", "HTTPS://Example.ru"),
        ("vk.com/club123", "https://vk.com/club123"),
        ("https://example.ru:8080/x", "https://example.ru:8080/x"),
    ],
)
def test_url_valid(raw, expected):
    assert validate_url(raw) == (True, expected, None)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "просто текст",
        "example .ru",
        "example",
        "https://",
        "ftp://example.ru",
        "javascript:alert(1)",
        "http://localhost",
        "example.r",
        "-bad-.ru",
        "https://example.ru/" + "a" * 500,
        "нет",
    ],
)
def test_url_invalid(raw):
    assert validate_url(raw) == (False, None, "err.url")


# --- several urls -----------------------------------------------------------

def test_urls_space_and_newline_separated():
    ok, value, err = validate_urls("vk.com/club1  https://t.me/chan\ninstagram.com/x,")
    assert (ok, err) == (True, None)
    assert value == "https://vk.com/club1\nhttps://t.me/chan\nhttps://instagram.com/x"


def test_urls_single_and_dedup():
    assert validate_urls("vk.com/a vk.com/a") == (True, "https://vk.com/a", None)


@pytest.mark.parametrize("raw", ["", "   ", None, "vk.com/a мусор", "просто текст"])
def test_urls_invalid(raw):
    assert validate_urls(raw) == (False, None, "err.url")


# --- free text --------------------------------------------------------------

def test_free_text_trimmed():
    assert validate_free_text("  Хочу сайт  ") == (True, "Хочу сайт", None)


def test_free_text_bounds():
    assert validate_free_text("ок") == (True, "ок", None)
    assert validate_free_text("a" * 1000) == (True, "a" * 1000, None)
    assert validate_free_text(" a ") == (False, None, "err.text_short")
    assert validate_free_text(None) == (False, None, "err.text_short")
    assert validate_free_text("a" * 1001) == (False, None, "err.text_long")


def test_free_text_custom_limits():
    assert validate_free_text("abc", min_len=5) == (False, None, "err.text_short")
    assert validate_free_text("abcdef", max_len=5) == (False, None, "err.text_long")


# --- file -------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["tz.pdf", "ТЗ.DOCX", "a.doc", "a.rtf", "a.odt", "a.txt", "a.xls", "a.xlsx",
     "a.ppt", "a.pptx", "a.png", "a.jpg", "a.jpeg", "a.zip"],
)
def test_file_allowed_extensions(name):
    assert validate_file(name, None, 1000, "document") == (True, name, None)


@pytest.mark.parametrize("name", ["virus.exe", "a.mp3", "noext", "a.pdf.exe"])
def test_file_bad_extension(name):
    assert validate_file(name, "application/octet-stream", 1000, "document") == (
        False, None, "err.file_type")


def test_file_without_name_uses_mime():
    assert validate_file(None, "application/pdf", 10, "document") == (True, "file.pdf", None)
    assert validate_file(None, "audio/mpeg", 10, "document") == (False, None, "err.file_type")


def test_photo_always_ok_type():
    assert validate_file(None, None, 50_000, "photo") == (True, "photo.jpg", None)


def test_file_size_limit():
    assert MAX_FILE_SIZE == 20 * MB
    assert validate_file("a.pdf", None, 20 * MB, "document")[0] is True
    assert validate_file("a.pdf", None, 20 * MB + 1, "document") == (False, None, "err.file_size")
    assert validate_file(None, None, 20 * MB + 1, "photo") == (False, None, "err.file_size")


def test_file_unknown_size_accepted():
    assert validate_file("a.pdf", None, None, "document")[0] is True


def test_file_unknown_kind_rejected():
    assert validate_file("a.pdf", None, 10, "voice") == (False, None, "err.file_type")
