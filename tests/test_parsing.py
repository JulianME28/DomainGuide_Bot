"""Тести розбору «брудних» даних.

Головна вимога: парсер не має падати НІКОЛИ, хоч що йому підсунуть.

Особливі пробіли задані через chr(код), а не самим символом. Причина проста:
нерозривний пробіл виглядає точнісінько як звичайний, і його дуже легко
випадково замінити під час редагування — тоді тест мовчки перестав би
перевіряти те, заради чого написаний.
"""

from __future__ import annotations

import pytest

from app.data.parsing import (
    extract_zone,
    normalize_domain,
    normalize_language,
    parse_geo,
    parse_number,
)

NBSP = chr(0x00A0)  # нерозривний пробіл — саме такий ставить Google Sheets
NARROW_NBSP = chr(0x202F)  # вузький нерозривний
THIN_SPACE = chr(0x2009)  # тонкий


class TestParseNumber:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("28", 28.0),
            ("0", 0.0),
            (42, 42.0),
            (3.5, 3.5),
            ("28.5", 28.5),
            ("28,5", 28.5),
        ],
    )
    def test_звичайні_числа(self, raw, expected):
        assert parse_number(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["n/a", "N/A", " n/a ", "", "   ", "-", "—", "#N/A", None, "null", "нема", "казна-що"],
    )
    def test_відсутнє_значення_дає_none(self, raw):
        assert parse_number(raw) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("4 800", 4800.0),  # звичайний пробіл
            (f"4{NBSP}800", 4800.0),  # нерозривний
            (f"4{NARROW_NBSP}800", 4800.0),  # вузький нерозривний
            (f"4{THIN_SPACE}800", 4800.0),  # тонкий
            ("4'800", 4800.0),  # швейцарський апостроф
            ("1,200", 1200.0),  # кома як роздільник тисяч
            ("1.200", 1200.0),  # крапка як роздільник тисяч
            ("1 234 567", 1234567.0),
            ("1,234.56", 1234.56),  # англійський формат
            ("1.234,56", 1234.56),  # європейський формат
        ],
    )
    def test_роздільники_тисяч(self, raw, expected):
        assert parse_number(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("12K", 12000.0), ("12k", 12000.0), ("1.5K", 1500.0), ("2M", 2_000_000.0)],
    )
    def test_множники(self, raw, expected):
        assert parse_number(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("<10", 10.0), ("~500", 500.0), (">100", 100.0), ("1200 відвідувань", 1200.0)],
    )
    def test_приблизні_позначки_і_хвости(self, raw, expected):
        assert parse_number(raw) == expected

    @pytest.mark.parametrize("raw", ["-5", "-100", -3])
    def test_відємні_вважаються_сміттям(self, raw):
        assert parse_number(raw) is None

    @pytest.mark.parametrize("raw", [True, False, [], {}, object()])
    def test_не_падає_на_будь_якому_смітті(self, raw):
        assert parse_number(raw) is None


class TestNormalizeLanguage:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("English", "english"),
            ("english ", "english"),
            ("  Turkish  ", "turkish"),
            ("Chinese   Simplified", "chinese simplified"),
            ("", ""),
            ("n/a", ""),
            (None, ""),
        ],
    )
    def test_нормалізація(self, raw, expected):
        assert normalize_language(raw) == expected

    def test_хвостовий_нерозривний_пробіл(self):
        """Найпідступніший випадок: на око «German » і «German» однакові."""
        assert normalize_language(f"German{NBSP}") == "german"
        assert normalize_language(f"{NBSP}Spanish{NARROW_NBSP}") == "spanish"


class TestParseGeo:
    """GEO у форматі `(cc, N)`. Не падає ніколи; невідповідне → («», None)."""

    @pytest.mark.parametrize(
        ("raw", "code", "traffic"),
        [
            ("(fr, 0)", "fr", 0.0),
            ("(us, 16640)", "us", 16640.0),
            ("(DE, 900)", "de", 900.0),  # код зводиться до нижнього регістру
            ("( gb , 5 )", "gb", 5.0),  # зайві пробіли
            ("(vn, 44116)", "vn", 44116.0),
        ],
    )
    def test_валідні_значення(self, raw, code, traffic):
        assert parse_geo(raw) == (code, traffic)

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            None,
            "fr, 0",  # без дужок
            "(fra, 100)",  # три літери
            "(fr)",  # без числа
            "(1, 2)",  # не літери
            "(fr, abc)",  # число не число
            "16, 10",  # це формат службової колонки raw, не GEO
            "казна-що",
        ],
    )
    def test_невідповідний_формат_це_немає_geo(self, raw):
        assert parse_geo(raw) == ("", None)

    def test_нуль_це_валідне_значення_а_не_відсутність(self):
        """(fr, 0) — код Є, трафік 0. «Не рахувати» вирішує вже модель, не парсер."""
        code, traffic = parse_geo("(fr, 0)")
        assert code == "fr"
        assert traffic == 0.0


class TestNormalizeDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Example.DE", "example.de"),
            ("https://www.example.co.uk/blog?x=1", "example.co.uk"),
            ("http://example.com", "example.com"),
            ("www.example.com", "example.com"),
            ("example.com:8080", "example.com"),
            ("  example.com  ", "example.com"),
            ("info@example.com", "example.com"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_чистка_домену(self, raw, expected):
        assert normalize_domain(raw) == expected


class TestExtractZone:
    @pytest.mark.parametrize(
        ("domain", "expected"),
        [
            ("example.de", ".de"),
            ("example.fr", ".fr"),
            ("shop.example.de", ".de"),
            ("mail.web.de", ".de"),  # "web.de" — не складена зона, це справжній сайт
            ("example.com", ".com"),
            ("example.co", ".co"),  # гола .co — глобальна зона
        ],
    )
    def test_прості_зони(self, domain, expected):
        assert extract_zone(domain) == expected

    @pytest.mark.parametrize(
        ("domain", "expected"),
        [
            ("bbc.co.uk", ".co.uk"),
            ("sub.bbc.co.uk", ".co.uk"),
            ("site.com.au", ".com.au"),
            ("site.com.br", ".com.br"),
            ("site.com.tr", ".com.tr"),
            ("site.co.za", ".co.za"),
            ("site.co.il", ".co.il"),
            ("site.co.id", ".co.id"),
            ("site.com.mx", ".com.mx"),
            ("site.com.ar", ".com.ar"),
            ("site.co.in", ".co.in"),
            ("site.com.co", ".com.co"),  # Колумбія, а не глобальна .co
        ],
    )
    def test_складені_зони(self, domain, expected):
        assert extract_zone(domain) == expected

    @pytest.mark.parametrize(
        "domain",
        ["беззони", "", None, "localhost", "192.168.0.1", "."],
    )
    def test_зону_визначити_неможливо(self, domain):
        assert extract_zone(domain) == ""

    def test_повний_url_теж_працює(self):
        assert extract_zone("https://WWW.Example.CO.UK/page") == ".co.uk"
