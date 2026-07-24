"""Тести розбору вільного тексту (без нейромережі)."""

from __future__ import annotations

import pytest

from app.analytics.query import QueryKind
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from app.text.freeform import detect_section, parse_free_text


class TestВизначенняБази:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("скільки донорів у Меджику", "magic"),
            ("Меджик, Британія", "magic"),
            ("по Мордах скільки", "mordy"),
            ("Морди, .de", "mordy"),
            ("сабміти", "submits"),
        ],
    )
    def test_база_названа(self, text, expected):
        section, named = detect_section(text)
        assert section == expected
        assert named

    def test_база_не_названа_беремо_меджик(self):
        section, named = detect_section("скільки донорів по Британії")
        assert section == "magic"
        assert not named


class TestРеальніЗапити:
    def test_запит_з_тз(self):
        """Приклад прямо з технічного завдання."""
        parsed = parse_free_text(
            "Скільки у нас донорів по Британії в Меджику з трафіком від 1, DR не важливий?"
        )

        assert parsed.understood
        assert parsed.query.section_key == "magic"
        assert parsed.query.country is country_by_code("gb")
        assert parsed.query.traffic_min == 1
        assert parsed.query.dr_min is None, "«DR не важливий» — фільтра бути не має"

    def test_запит_по_мові(self):
        parsed = parse_free_text("Скільки французькомовних донорів з трафіком від 5?")

        assert parsed.query.language is language_by_code("fr")
        assert parsed.query.country is None
        assert parsed.query.traffic_min == 5
        assert parsed.query.kind is QueryKind.LANGUAGE

    def test_запит_по_країні(self):
        parsed = parse_free_text("Меджик, Франція, трафік від 10, DR від 20")

        assert parsed.query.country is country_by_code("fr")
        assert parsed.query.traffic_min == 10
        assert parsed.query.dr_min == 20
        assert parsed.query.kind is QueryKind.COUNTRY

    def test_запит_тільки_по_метриках(self):
        parsed = parse_free_text("Скільки донорів у Меджику з DR від 30 і трафіком від 100?")

        assert parsed.query.dr_min == 30
        assert parsed.query.traffic_min == 100
        assert parsed.query.country is None
        assert parsed.query.kind is QueryKind.METRICS

    def test_запит_із_зоною(self):
        """«в зоні .de» — це модифікатор ЗОНИ: рахуємо лише зону, без водоспаду."""
        parsed = parse_free_text("донори в зоні .de з трафіком від 50")
        assert parsed.query.zones == (".de",)
        assert parsed.query.country is None, "«у зоні X» — не країновий запит"
        assert parsed.query.kind is QueryKind.ZONE
        assert parsed.query.traffic_min == 50


class TestЧисла:
    @pytest.mark.parametrize(
        ("text", "field", "expected"),
        [
            ("трафік від 100", "traffic_min", 100),
            ("трафік від 1 200", "traffic_min", 1200),
            ("DR від 20", "dr_min", 20),
            ("DR до 40", "dr_max", 40),
            ("трафік до 500", "traffic_max", 500),
        ],
    )
    def test_окремі_фільтри(self, text, field, expected):
        parsed = parse_free_text(text)
        assert getattr(parsed.query, field) == expected

    def test_діапазон(self):
        parsed = parse_free_text("DR від 20 до 40")
        assert parsed.query.dr_min == 20
        assert parsed.query.dr_max == 40

    def test_два_фільтри_не_плутаються(self):
        """Числа мають прив'язатися кожне до свого ключового слова."""
        parsed = parse_free_text("трафік від 100, DR від 30")
        assert parsed.query.traffic_min == 100
        assert parsed.query.dr_min == 30

    @pytest.mark.parametrize(
        "text",
        ["DR не важливий", "DR не важлива", "DR без обмежень", "DR байдуже"],
    )
    def test_фільтр_вимкнено_словами(self, text):
        parsed = parse_free_text(text)
        assert parsed.query.dr_min is None
        assert parsed.query.dr_max is None

    def test_число_без_ключового_слова_ігнорується(self):
        """«від 10» саме по собі незрозуміле — краще перепитати."""
        parsed = parse_free_text("донори по Британії від 10")
        assert parsed.query.dr_min is None
        assert parsed.query.traffic_min is None


class TestНезрозумілийЗапит:
    @pytest.mark.parametrize(
        "text",
        ["привіт", "asdfgh", "", "   ", "що ти вмієш"],
    )
    def test_потрібне_уточнення(self, text):
        parsed = parse_free_text(text)
        assert parsed.needs_clarification
        assert not parsed.understood

    def test_навіть_сама_назва_бази_вже_щось(self):
        parsed = parse_free_text("Меджик")
        assert parsed.understood
        assert parsed.section_named


class TestРозбірНеПадає:
    @pytest.mark.parametrize(
        "text",
        [
            "??????",
            "DR від",
            "трафік від до",
            "0" * 500,
            "Німеччина " * 100,
            "🇩🇪🇫🇷🇬🇧",
            "<script>alert(1)</script>",
        ],
    )
    def test_будь_який_текст_розбирається_без_винятку(self, text):
        parsed = parse_free_text(text)
        assert parsed.query is not None


class TestФразиПрохання:
    """Маркери-прохання («якщо мало», «альтернативи») — це підказка для
    рекомендацій, а НЕ фільтр. Слова після/навколо них не звужують запит."""

    def test_приклад_із_тз(self):
        """«Нова Зеландія; якщо мало — англомовні альтернативи» — країновий запит
        БЕЗ мовного фільтра + фраза-прохання окремо."""
        parsed = parse_free_text("Донори по Новій Зеландії; якщо мало — англомовні альтернативи")
        assert parsed.query.country is country_by_code("nz")
        assert parsed.query.language is None, "«англомовні» не має ставати фільтром"
        assert parsed.request_marker is True
        assert parsed.query.request_hint == "англомовні альтернативи"
        assert parsed.query.kind is QueryKind.COUNTRY

    @pytest.mark.parametrize(
        "text",
        [
            "Нова Зеландія; якщо мало — англомовні альтернативи",
            "Нова Зеландія; якщо замало, англомовні варіанти",
            "Нова Зеландія; якщо недостатньо — англомовні",
            "Нова Зеландія; англомовні альтернативи",
            "Нова Зеландія; запропонуй схожі",
            "Нова Зеландія; підбери схожі",
            "Нова Зеландія; схожі варіанти",
            "Нова Зеландія; що ще є",
        ],
    )
    def test_маркер_розпізнається_у_різних_формах(self, text):
        parsed = parse_free_text(text)
        assert parsed.request_marker is True
        assert parsed.query.country is country_by_code("nz")
        assert parsed.query.language is None

    def test_кома_замість_крапки_з_комою(self):
        """«Німеччина, варіанти» — країна лишається, «варіанти» йде в прохання."""
        parsed = parse_free_text("Німеччина, варіанти")
        assert parsed.query.country is country_by_code("de")
        assert parsed.request_marker is True
        assert parsed.query.request_hint == "варіанти"

    def test_англійською_без_маркера_це_фільтр(self):
        """«Морди англійською» без прохання й далі означає фільтр мови."""
        parsed = parse_free_text("Морди англійською")
        assert parsed.query.language is language_by_code("en")
        assert parsed.request_marker is False
        assert parsed.query.request_hint == ""

    def test_звичайний_запит_не_чіпається(self):
        """Без маркера текст не ділиться й коми зберігають сенс."""
        parsed = parse_free_text("Меджик, Британія, трафік від 1")
        assert parsed.query.country is country_by_code("gb")
        assert parsed.query.traffic_min == 1
        assert parsed.request_marker is False

    def test_прохання_це_зрозумілий_запит(self):
        """Навіть якщо крім прохання нічого не назвали — це не «не зрозумів»."""
        parsed = parse_free_text("Франція; підбери схожі")
        assert not parsed.needs_clarification
