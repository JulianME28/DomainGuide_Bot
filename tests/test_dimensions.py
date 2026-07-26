"""Тести спільного механізму розбору вимірів.

═══════════════════════════════════════════════════════════════════════════
ЩО ТУТ ЗАХИЩАЄТЬСЯ
═══════════════════════════════════════════════════════════════════════════

Один і той самий баг вилазив двічі: спершу для DR, потім для трафіку.
Слово «будь-яка» від КРАЇНИ забруднювало вікно пошуку сусідньої метрики:

    «будь-яка країна др від 50»      → DR не розпізнавався
    «будь-яка країна трафік від 50»  → трафік не розпізнавався

Причина була структурна: кожен вимір розбирався своїм кодом за своїми
правилами. Полагодивши один, легко забути про інший — і про наступний.

Тепер усі виміри описані одним переліком SPECS і розбираються одним кодом.
Тести нижче перебирають УСІ пари вимірів, тому дірка для наступного виміру
не пройде непоміченою.
"""

from __future__ import annotations

import pytest

from app.analytics.query import Dimension
from app.dictionary.normalize import normalize_text
from app.text.dimensions import SPECS, active_dimensions, resolve_dimensions
from app.text.freeform import parse_free_text


def parse(text: str):
    return parse_free_text(text)


# ---------------------------------------------------------------------------
# П'ять форм із заявки на баг
# ---------------------------------------------------------------------------


class TestПʼятьФормІзБага:
    """Кожна форма перевіряється точно так, як її написала власниця."""

    def test_без_коми(self):
        """Саме ця форма й ламалася."""
        parsed = parse("будь-яка країна трафік від 50")

        assert parsed.understood
        assert parsed.query.traffic_min == 50
        assert parsed.query.country is None
        assert Dimension.COUNTRY in parsed.cancelled

    def test_з_комою(self):
        parsed = parse("будь-яка країна, трафік від 50")

        assert parsed.understood
        assert parsed.query.traffic_min == 50
        assert parsed.query.country is None

    def test_сам_трафік(self):
        parsed = parse("трафік від 50")

        assert parsed.understood
        assert parsed.query.traffic_min == 50
        assert Dimension.COUNTRY not in parsed.cancelled

    def test_dr_як_минулого_разу(self):
        parsed = parse("будь-яка країна др від 50")

        assert parsed.understood
        assert parsed.query.dr_min == 50
        assert parsed.query.country is None

    def test_дві_метрики_поспіль(self):
        """Найскладніша форма: скасування плюс дві метрики без коми."""
        parsed = parse("будь-яка країна трафік від 50 др від 20")

        assert parsed.understood
        assert parsed.query.traffic_min == 50, "трафік читає свій шматок"
        assert parsed.query.dr_min == 20, "DR читає свій"
        assert parsed.query.country is None


# ---------------------------------------------------------------------------
# Перехресна перевірка: скасування X не чіпає метрику Y
# ---------------------------------------------------------------------------

# Фрази скасування для кожного виміру — зокрема й для тих, що поки без колонок.
CANCEL_PHRASES: dict[str, tuple[str, ...]] = {
    Dimension.COUNTRY: ("будь-яка країна", "всі країни", "країна не важлива"),
    Dimension.LANGUAGE: ("будь-яка мова", "всі мови", "мова не важлива", "без урахування мови"),
    Dimension.TRAFFIC: ("будь-який трафік", "трафік не важливий", "трафік без обмежень"),
    Dimension.DR: ("будь-який dr", "dr не важливий", "др не важливий"),
    Dimension.OUTLINKS: ("будь-які вихідні лінки", "вихідні лінки не важливі"),
    Dimension.SPAM: ("будь-яка заспамленість", "заспамленість не важлива"),
}

# Числові фільтри: (текст, вимір, очікуване значення). Усі чотири числові
# виміри — щоб перехрестя покривало й заспамленість нарівні з DR і трафіком.
METRIC_FILTERS: tuple[tuple[str, str, float], ...] = (
    ("трафік від 50", Dimension.TRAFFIC, 50.0),
    ("др від 20", Dimension.DR, 20.0),
    ("dr від 30", Dimension.DR, 30.0),
    ("вихідних лінків від 15", Dimension.OUTLINKS, 15.0),
    ("заспамленість від 40", Dimension.SPAM, 40.0),
)

_VALUE_FIELD = {
    Dimension.TRAFFIC: "traffic_min",
    Dimension.DR: "dr_min",
    Dimension.OUTLINKS: "outlinks_min",
    Dimension.SPAM: "spam_min",
}


def value_of(parsed, dimension: str) -> float | None:
    return getattr(parsed.query, _VALUE_FIELD[dimension])


CROSS_PAIRS = [
    (phrase, cancel_dim, filter_text, filter_dim, expected)
    for cancel_dim, phrases in CANCEL_PHRASES.items()
    for phrase in phrases
    for filter_text, filter_dim, expected in METRIC_FILTERS
    if cancel_dim != filter_dim
]


class TestСкасуванняНеЧіпаєСусіда:
    """Головний захист: усі пари «скасування X + метрика Y», БЕЗ коми."""

    @pytest.mark.parametrize(
        ("phrase", "cancel_dim", "filter_text", "filter_dim", "expected"),
        CROSS_PAIRS,
        ids=lambda v: str(v).replace(" ", "_") if isinstance(v, str) else str(v),
    )
    def test_скасування_перед_метрикою(self, phrase, cancel_dim, filter_text, filter_dim, expected):
        parsed = parse(f"{phrase} {filter_text}")

        assert value_of(parsed, filter_dim) == expected, (
            f"«{phrase}» не мало вплинути на «{filter_text}»"
        )
        assert parsed.understood

    @pytest.mark.parametrize(
        ("phrase", "cancel_dim", "filter_text", "filter_dim", "expected"),
        CROSS_PAIRS,
        ids=lambda v: str(v).replace(" ", "_") if isinstance(v, str) else str(v),
    )
    def test_метрика_перед_скасуванням(self, phrase, cancel_dim, filter_text, filter_dim, expected):
        parsed = parse(f"{filter_text} {phrase}")

        assert value_of(parsed, filter_dim) == expected, (
            f"«{phrase}» не мало вплинути на «{filter_text}»"
        )

    @pytest.mark.parametrize(
        ("phrase", "cancel_dim"),
        [(p, d) for d, phrases in CANCEL_PHRASES.items() for p in phrases],
    )
    def test_скасування_саме_по_собі_працює(self, phrase, cancel_dim):
        """Кожна фраза має спрацьовувати й окремо, без сусідів."""
        matches, _ = resolve_dimensions(normalize_text(phrase))

        assert cancel_dim in matches
        assert matches[cancel_dim].cancelled


class TestДвіМетрикиПоспіль:
    """Кожен вимір читає лише свій шматок тексту."""

    @pytest.mark.parametrize(
        "text",
        [
            "трафік від 50 др від 20",
            "др від 20 трафік від 50",
            "трафік від 50 dr від 20",
            "будь-яка країна трафік від 50 др від 20",
            "всі мови трафік від 50 др від 20",
        ],
    )
    def test_обидві_метрики_читаються(self, text):
        parsed = parse(text)
        assert parsed.query.traffic_min == 50
        assert parsed.query.dr_min == 20

    def test_одна_метрика_задана_друга_знята(self):
        parsed = parse("трафік від 100 будь-який dr")

        assert parsed.query.traffic_min == 100
        assert parsed.query.dr_min is None
        assert Dimension.DR in parsed.cancelled

    def test_діапазони_не_плутаються(self):
        parsed = parse("трафік від 10 до 90 др від 20 до 40")

        assert (parsed.query.traffic_min, parsed.query.traffic_max) == (10, 90)
        assert (parsed.query.dr_min, parsed.query.dr_max) == (20, 40)


class TestНапрямПорогу:
    """«від» = мінімум ЗАВЖДИ; заперечення не інвертує напрям мовчки."""

    def test_від_це_мінімум(self):
        parsed = parse("DR від 50")
        assert parsed.query.dr_min == 50
        assert parsed.query.dr_max is None

    def test_два_від_обидва_мінімуми(self):
        """Ключове: два пороги «від» в одному запиті — обидва мінімуми."""
        parsed = parse("DR від 50 і трафік від 50")
        assert (parsed.query.dr_min, parsed.query.dr_max) == (50, None)
        assert (parsed.query.traffic_min, parsed.query.traffic_max) == (50, None)

    def test_до_це_максимум(self):
        parsed = parse("трафік до 100")
        assert (parsed.query.traffic_min, parsed.query.traffic_max) == (None, 100)

    def test_не_менше_це_мінімум(self):
        """«не менше 50» = щонайменше 50, а не «до 50» (була інверсія)."""
        parsed = parse("DR не менше 50")
        assert parsed.query.dr_min == 50
        assert parsed.query.dr_max is None

    def test_не_більше_це_максимум(self):
        """«не більше 50» = максимум 50 (раніше давало і min, і max)."""
        parsed = parse("DR не більше 50")
        assert parsed.query.dr_max == 50
        assert parsed.query.dr_min is None

    @pytest.mark.parametrize(
        ("text", "expected_min"),
        [("DR від 50", 50), ("DR понад 50", 50), ("DR більше 50", 50), ("DR не менше 50", 50)],
    )
    def test_синоніми_мінімуму(self, text, expected_min):
        parsed = parse(text)
        assert parsed.query.dr_min == expected_min
        assert parsed.query.dr_max is None


class TestВимірЧитаєЛишеСвійШматок:
    """Число з чужого шматка не має перетікати до сусіда.

    Це друга половина захисту: не лише слово скасування не чіпає сусіда,
    а й число. Вікно виміру обрізається на назві наступного виміру —
    зокрема й на назві країни чи мови між двома метриками.
    """

    def test_країна_між_метриками_не_плутає_числа(self):
        parsed = parse("трафік від 50 Німеччина др від 20")

        assert parsed.query.traffic_min == 50
        assert parsed.query.dr_min == 20
        assert parsed.query.country is not None

    def test_мова_між_метриками_не_плутає_числа(self):
        parsed = parse("трафік від 50 англійською др від 20")

        assert parsed.query.traffic_min == 50
        assert parsed.query.dr_min == 20
        assert parsed.query.language is not None

    def test_діапазон_не_перетікає_в_сусідню_метрику(self):
        parsed = parse("dr від 20 до 40 трафік від 100")

        assert (parsed.query.dr_min, parsed.query.dr_max) == (20, 40)
        assert parsed.query.traffic_min == 100
        assert parsed.query.traffic_max is None, "верхня межа DR не стала межею трафіку"

    def test_число_перед_назвою_не_підхоплюється(self):
        """«50 трафік» — число стоїть до назви, це не фільтр."""
        assert parse("50 трафік").query.traffic_min is None

    def test_число_поруч_із_країною_без_метрики_ігнорується(self):
        parsed = parse("Німеччина 2024")

        assert parsed.query.country is not None
        assert parsed.query.traffic_min is None
        assert parsed.query.dr_min is None


class TestЧислоСильнішеЗаСлово:
    """Правило однакове для всіх вимірів: явне число перемагає «будь-який»."""

    @pytest.mark.parametrize(
        ("text", "dimension", "expected"),
        [
            ("будь-який трафік від 50", Dimension.TRAFFIC, 50.0),
            ("будь-який dr від 30", Dimension.DR, 30.0),
            ("трафік будь-який від 50", Dimension.TRAFFIC, 50.0),
        ],
    )
    def test_число_перемагає(self, text, dimension, expected):
        assert value_of(parse(text), dimension) == expected

    def test_слово_діє_коли_числа_немає(self):
        parsed = parse("трафік не важливий")

        assert parsed.query.traffic_min is None
        assert Dimension.TRAFFIC in parsed.cancelled


# ---------------------------------------------------------------------------
# Сам механізм
# ---------------------------------------------------------------------------


class TestМеханізм:
    def test_перелік_вимірів_єдиний(self):
        """SPECS — єдине місце, де описані виміри. Дублікатів бути не має."""
        dimensions = [spec.dimension for spec in SPECS]
        assert len(dimensions) == len(set(dimensions))

    def test_усі_виміри_активні(self):
        """Після підключення заспамленості вимкнених вимірів не лишилося."""
        assert active_dimensions() == {
            Dimension.COUNTRY,
            Dimension.LANGUAGE,
            Dimension.ZONE,
            Dimension.TRAFFIC,
            Dimension.DR,
            Dimension.OUTLINKS,
            Dimension.SPAM,
        }
        assert all(spec.active for spec in SPECS)

    def test_фраза_скасування_прибирається_з_тексту(self):
        """Інакше словник сплутав би «країна» з назвою країни."""
        _, remaining = resolve_dimensions(normalize_text("будь-яка країна трафік від 50"))
        assert "країна" not in remaining

    def test_число_метрики_лишається_але_не_заважає(self):
        """Число не затирається — воно не назва країни й нікому не шкодить."""
        matches, remaining = resolve_dimensions(normalize_text("трафік від 50 Німеччина др від 20"))
        # «Німеччина» між метриками має вціліти для словника.
        assert "німеччина" in remaining
        assert matches[Dimension.TRAFFIC].minimum == 50
        assert matches[Dimension.DR].minimum == 20

    def test_нерозібране_лишається(self):
        _, remaining = resolve_dimensions(normalize_text("донори по німеччині"))
        assert "німеччині" in remaining

    def test_вихідні_лінки_не_ламають_сусіда(self):
        """Скасування вихідних лінків не чіпає сусідню метрику."""
        parsed = parse("будь-які вихідні лінки трафік від 50")
        assert parsed.query.traffic_min == 50
        assert Dimension.OUTLINKS in parsed.cancelled

    def test_вихідні_лінки_тепер_у_фільтрах(self):
        """Вимір підключений — фраза скасування потрапляє в mentioned."""
        parsed = parse("будь-які вихідні лінки")
        assert Dimension.OUTLINKS in parsed.mentioned
        assert Dimension.OUTLINKS in parsed.cancelled


class TestНазвиВимірівУСловах:
    """Назви вимірів шукаються за початком слова, з межею на початку."""

    def test_мова_не_знаходиться_всередині_слова(self):
        """«англомовні» містить «мов», але це не згадка виміру «мова»."""
        parsed = parse("англомовні донори")

        assert parsed.query.language is not None, "мова має розпізнатися словником"
        assert Dimension.LANGUAGE not in parsed.cancelled

    @pytest.mark.parametrize(
        "text",
        ["трафіком від 50", "трафіку від 50", "по трафіку від 50"],
    )
    def test_відмінки_метрики(self, text):
        assert parse(text).query.traffic_min == 50

    @pytest.mark.parametrize(
        "text",
        ["будь-якій країні", "будь-яку країну", "будь-якою країною", "по всіх країнах"],
    )
    def test_відмінки_скасування(self, text):
        assert Dimension.COUNTRY in parse(text).cancelled


class TestНічогоНеЗламано:
    """Форми, які працювали раніше, мають працювати далі."""

    def test_запит_з_тз(self):
        parsed = parse(
            "Скільки у нас донорів по Британії в Меджику з трафіком від 1, DR не важливий?"
        )
        assert parsed.query.country is not None
        assert parsed.query.traffic_min == 1
        assert parsed.query.dr_min is None

    def test_число_без_назви_метрики_ігнорується(self):
        parsed = parse("донори по Британії від 10")
        assert parsed.query.traffic_min is None
        assert parsed.query.dr_min is None

    def test_незрозумілий_запит(self):
        assert parse("привіт").needs_clarification

    @pytest.mark.parametrize(
        "text",
        ["??????", "DR від", "трафік від до", "0" * 300, "<script>alert(1)</script>", "🇩🇪"],
    )
    def test_не_падає_на_смітті(self, text):
        assert parse(text).query is not None
