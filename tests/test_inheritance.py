"""Тести успадкування фільтрів між запитами.

═══════════════════════════════════════════════════════════════════════════
ЩО ТУТ ЗАХИЩАЄТЬСЯ
═══════════════════════════════════════════════════════════════════════════

Реальний баг із роботи. Сценарій:

    1. запит «англомовні донори»           → мова = англійська
    2. новий запит «будь-яка країна др від 50»

Друге повідомлення бот не розумів узагалі: слово «будь-яка» від КРАЇНИ
потрапляло у вікно пошуку фільтра DR і вимикало його. Нічого не
розпізналось → стан не оновився → у резюме тихо лишалась англійська мова
з першого запиту. Числа виходили не ті, і помітити це можна було лише
випадково.

За ТЗ (розділ 29) бот МАЄ пам'ятати запит до скидання — цю пам'ять не
чіпаємо. Проблема була не в пам'яті, а в тому, що успадкування невидиме
й важко скасовне. Тому:

    * успадкований фільтр підписується «(з попереднього запиту)»;
    * кожен такий фільтр знімається окремою кнопкою;
    * словами теж можна: «всі мови», «будь-яка країна», «DR не важливий»;
    * країна + чужа мова дають помітне попередження.
"""

from __future__ import annotations

import pytest

from app.analytics.query import Dimension, DonorQuery
from app.bot.keyboards import wizard_confirm
from app.bot.states import (
    FRESH_KEY,
    INHERITED_MARK,
    conflict_warning,
    fresh_from_state,
    inherited_dimensions,
    query_from_state,
    query_to_state,
    summary_lines,
)
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from app.text.freeform import parse_free_text

# ---------------------------------------------------------------------------
# 1. Позначення успадкованих фільтрів
# ---------------------------------------------------------------------------


class TestПозначенняУспадкованого:
    def test_успадкований_фільтр_підписано(self):
        """Мова прийшла з минулого запиту — має бути підпис."""
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
        )
        text = summary_lines(query, "Меджик", fresh=frozenset({Dimension.COUNTRY}))

        language_line = next(line for line in text.split("\n") if "Мова:" in line)
        assert INHERITED_MARK in language_line

    def test_заданий_щойно_фільтр_без_підпису(self):
        """Країну обрали в цьому кроці — підпису бути не має."""
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
        )
        text = summary_lines(query, "Меджик", fresh=frozenset({Dimension.COUNTRY}))

        country_line = next(line for line in text.split("\n") if "Країна:" in line)
        assert INHERITED_MARK not in country_line

    def test_порожній_фільтр_не_підписується(self):
        """«не обрано» не може бути успадкованим — там нічого немає."""
        query = DonorQuery(section_key="magic", dr_min=50)
        text = summary_lines(query, "Меджик", fresh=frozenset({Dimension.DR}))

        assert INHERITED_MARK not in text

    def test_підпис_пояснено_окремим_рядком(self):
        query = DonorQuery(section_key="magic", language=language_by_code("en"))
        text = summary_lines(query, "Меджик", fresh=frozenset())

        assert "лишилися з попереднього запиту" in text

    def test_якщо_нічого_не_успадковано_пояснення_немає(self):
        query = DonorQuery(section_key="magic", dr_min=50)
        text = summary_lines(query, "Меджик", fresh=frozenset({Dimension.DR}))

        assert "лишилися з попереднього" not in text

    @pytest.mark.parametrize(
        ("dimension", "label"),
        [
            (Dimension.COUNTRY, "Країна:"),
            (Dimension.LANGUAGE, "Мова:"),
            (Dimension.TRAFFIC, "Трафік:"),
            (Dimension.DR, "DR:"),
        ],
    )
    def test_підписується_будь_який_вимір(self, dimension, label):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("de"),
            traffic_min=100,
            dr_min=30,
        )
        # Свіже все, крім одного виміру, — саме він і має бути підписаний.
        fresh = query.filled_dimensions - {dimension}
        text = summary_lines(query, "Меджик", fresh=fresh)

        marked = [line for line in text.split("\n") if INHERITED_MARK in line]
        assert len(marked) == 1
        assert label in marked[0]


class TestОблікСвіжості:
    def test_виміри_рахуються_правильно(self):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
            dr_min=30,
        )
        assert query.filled_dimensions == {
            Dimension.COUNTRY,
            Dimension.LANGUAGE,
            Dimension.DR,
        }

    def test_успадковане_це_заповнене_мінус_свіже(self):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
        )
        inherited = inherited_dimensions(query, frozenset({Dimension.COUNTRY}))
        assert inherited == {Dimension.LANGUAGE}

    def test_свіжість_зберігається_і_читається(self):
        query = DonorQuery(section_key="magic", country=country_by_code("de"))
        data = query_to_state(query, frozenset({Dimension.COUNTRY}))

        assert fresh_from_state(data) == {Dimension.COUNTRY}

    def test_за_замовчуванням_усе_свіже(self):
        """Звичайний новий запит: усе, що в ньому є, задано щойно."""
        query = DonorQuery(section_key="magic", country=country_by_code("de"), dr_min=30)
        data = query_to_state(query)

        assert fresh_from_state(data) == {Dimension.COUNTRY, Dimension.DR}
        assert inherited_dimensions(query_from_state(data), fresh_from_state(data)) == frozenset()

    def test_у_памʼяті_лише_прості_типи(self):
        data = query_to_state(DonorQuery(section_key="magic"), frozenset({Dimension.DR}))
        assert isinstance(data[FRESH_KEY], list)
        assert all(isinstance(item, str) for item in data[FRESH_KEY])


# ---------------------------------------------------------------------------
# 2. Кнопки скидання окремих фільтрів
# ---------------------------------------------------------------------------


class TestКнопкиСкидання:
    def _callbacks(self, markup) -> list[str]:
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_кнопка_зʼявляється_для_успадкованого(self):
        markup = wizard_confirm({Dimension.LANGUAGE})
        assert "wizard:drop:language" in self._callbacks(markup)

    def test_кнопки_немає_для_незаданого(self):
        markup = wizard_confirm({Dimension.LANGUAGE})
        callbacks = self._callbacks(markup)

        assert "wizard:drop:country" not in callbacks
        assert "wizard:drop:traffic" not in callbacks
        assert "wizard:drop:dr" not in callbacks

    def test_без_успадкованого_кнопок_скидання_немає(self):
        callbacks = self._callbacks(wizard_confirm())
        assert not any(c.startswith("wizard:drop:") for c in callbacks)

    def test_повне_скидання_лишається(self):
        """Кнопка «Скинути все» має бути завжди — і з кнопками, і без."""
        assert "wizard:reset" in self._callbacks(wizard_confirm())
        assert "wizard:reset" in self._callbacks(wizard_confirm({Dimension.LANGUAGE}))

    def test_текст_кнопки_зрозумілий(self):
        markup = wizard_confirm({Dimension.LANGUAGE, Dimension.COUNTRY})
        texts = [b.text for row in markup.inline_keyboard for b in row]

        assert "❌ Прибрати мову" in texts
        assert "❌ Прибрати країну" in texts

    def test_callback_вкладається_в_ліміт_телеграма(self):
        markup = wizard_confirm(
            {Dimension.COUNTRY, Dimension.LANGUAGE, Dimension.TRAFFIC, Dimension.DR}
        )
        for callback in self._callbacks(markup):
            assert len(callback.encode("utf-8")) <= 64


class TestСкиданняОдногоВиміру:
    def test_прибирає_лише_свій_фільтр(self):
        """Головна вимога: сусідні фільтри мають лишитися недоторканими."""
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
            dr_min=50,
            traffic_min=100,
        )
        without_language = query.without(Dimension.LANGUAGE)

        assert without_language.language is None
        assert without_language.country is country_by_code("de")
        assert without_language.dr_min == 50
        assert without_language.traffic_min == 100

    @pytest.mark.parametrize(
        ("dimension", "field"),
        [
            (Dimension.COUNTRY, "country"),
            (Dimension.LANGUAGE, "language"),
            (Dimension.TRAFFIC, "traffic_min"),
            (Dimension.DR, "dr_min"),
        ],
    )
    def test_кожен_вимір_прибирається(self, dimension, field):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
            traffic_min=100,
            dr_min=50,
        )
        assert getattr(query.without(dimension), field) is None

    def test_прибирання_країни_знімає_і_зони(self):
        query = DonorQuery(section_key="magic", zones=(".com",))
        assert query.without(Dimension.COUNTRY).zones == ()

    def test_прибирання_метрики_знімає_обидві_межі(self):
        query = DonorQuery(section_key="magic", dr_min=20, dr_max=50)
        without = query.without(Dimension.DR)

        assert without.dr_min is None
        assert without.dr_max is None

    def test_початковий_запит_не_змінюється(self):
        query = DonorQuery(section_key="magic", language=language_by_code("en"))
        query.without(Dimension.LANGUAGE)
        assert query.language is language_by_code("en")


# ---------------------------------------------------------------------------
# 3. Фрази скасування у вільному тексті
# ---------------------------------------------------------------------------


class TestФразиСкасуванняМови:
    @pytest.mark.parametrize(
        "text",
        [
            "всі мови", "усі мови", "будь-яка мова", "будь-якою мовою",
            "мова не важлива", "без урахування мови", "незалежно від мови",
            "Меджик, всі мови, DR від 30",
        ],
    )  # fmt: skip
    def test_мова_обнуляється(self, text):
        parsed = parse_free_text(text)

        assert parsed.query.language is None
        assert Dimension.LANGUAGE in parsed.cancelled
        assert Dimension.LANGUAGE in parsed.mentioned
        assert parsed.understood, "скасування — теж зрозумілий намір"

    def test_скасування_перемагає_назву_мови(self):
        """Якщо сказали «всі мови», мова не має проскочити іншим шляхом."""
        assert parse_free_text("всі мови, трафік від 10").query.language is None


class TestФразиСкасуванняКраїни:
    @pytest.mark.parametrize(
        "text",
        [
            "будь-яка країна", "всі країни", "усі країни", "будь-яку країну",
            "країна не важлива", "без урахування країни", "без гео",
            "будь-яка країна, DR від 50",
        ],
    )  # fmt: skip
    def test_країна_обнуляється(self, text):
        parsed = parse_free_text(text)

        assert parsed.query.country is None
        assert Dimension.COUNTRY in parsed.cancelled
        assert parsed.understood

    def test_скасування_країни_знімає_і_зони(self):
        parsed = parse_free_text("будь-яка країна, .com")
        assert parsed.query.zones == ()


class TestФразиСкасуванняМетрик:
    @pytest.mark.parametrize(
        "text",
        ["DR не важливий", "DR будь-який", "будь-який DR", "DR без обмежень", "DR байдуже"],
    )
    def test_dr_обнуляється(self, text):
        parsed = parse_free_text(text)

        assert parsed.query.dr_min is None
        assert parsed.query.dr_max is None
        assert Dimension.DR in parsed.cancelled

    @pytest.mark.parametrize(
        "text",
        ["трафік не важливий", "трафік будь-який", "будь-який трафік", "трафік без обмежень"],
    )
    def test_трафік_обнуляється(self, text):
        parsed = parse_free_text(text)

        assert parsed.query.traffic_min is None
        assert Dimension.TRAFFIC in parsed.cancelled


class TestЧислоСильнішеЗаСлово:
    """Корінь бага: слово «будь-яка» від країни вимикало фільтр DR."""

    def test_будь_яка_країна_не_вимикає_dr(self):
        parsed = parse_free_text("будь-яка країна др від 50")

        assert parsed.query.dr_min == 50, "число має перемогти слово «будь-яка»"
        assert parsed.query.country is None
        assert parsed.understood

    def test_те_саме_з_комою(self):
        parsed = parse_free_text("будь-яка країна, др від 50")
        assert parsed.query.dr_min == 50

    def test_будь_яка_мова_не_вимикає_трафік(self):
        parsed = parse_free_text("будь-яка мова трафік від 100")
        assert parsed.query.traffic_min == 100

    def test_явне_число_поруч_із_чужим_скасуванням(self):
        parsed = parse_free_text("трафік від 100 будь-який dr")

        assert parsed.query.traffic_min == 100, "трафік задано числом"
        assert parsed.query.dr_min is None, "а DR справді знято"

    def test_слово_діє_коли_числа_немає(self):
        parsed = parse_free_text("трафік від 10, DR не важливий")

        assert parsed.query.traffic_min == 10
        assert parsed.query.dr_min is None


class TestСтаріФразиНеЗламані:
    """Те, що вже працювало, має працювати далі."""

    def test_запит_з_тз(self):
        parsed = parse_free_text(
            "Скільки у нас донорів по Британії в Меджику з трафіком від 1, DR не важливий?"
        )

        assert parsed.query.country is country_by_code("gb")
        assert parsed.query.traffic_min == 1
        assert parsed.query.dr_min is None

    def test_запит_по_мові(self):
        parsed = parse_free_text("Скільки французькомовних донорів з трафіком від 5?")

        assert parsed.query.language is language_by_code("fr")
        assert parsed.query.traffic_min == 5

    def test_діапазон(self):
        parsed = parse_free_text("DR від 20 до 40")
        assert (parsed.query.dr_min, parsed.query.dr_max) == (20, 40)

    def test_незрозумілий_запит_і_далі_незрозумілий(self):
        assert parse_free_text("привіт").needs_clarification
        assert parse_free_text("asdfgh").needs_clarification


# ---------------------------------------------------------------------------
# 4. Попередження про конфлікт країни й мови
# ---------------------------------------------------------------------------


class TestПопередженняПроКонфлікт:
    def test_чужа_мова_дає_попередження(self):
        """Німеччина + англійська: основна мова Німеччини — німецька."""
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
        )
        assert query.has_language_conflict

        warning = conflict_warning(query)
        assert "звужують вибірку" in warning
        assert "Німеччина" in warning
        assert "англійська" in warning
        assert "німецька" in warning

    def test_рідна_мова_попередження_не_дає(self):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("de"),
        )
        assert not query.has_language_conflict
        assert conflict_warning(query) == ""

    def test_британія_і_англійська_це_не_конфлікт(self):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("gb"),
            language=language_by_code("en"),
        )
        assert not query.has_language_conflict

    def test_без_країни_конфлікту_немає(self):
        query = DonorQuery(section_key="magic", language=language_by_code("en"))
        assert not query.has_language_conflict

    def test_без_мови_конфлікту_немає(self):
        query = DonorQuery(section_key="magic", country=country_by_code("de"))
        assert not query.has_language_conflict

    def test_попередження_видно_в_резюме(self):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
        )
        text = summary_lines(query, "Меджик", fresh=frozenset({Dimension.COUNTRY}))

        assert "⚠️" in text
        assert "звужують вибірку" in text
        assert "приберіть мову" in text.lower()

    def test_поруч_із_попередженням_є_кнопка(self):
        """Попередження без кнопки — порада без способу її виконати."""
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
        )
        inherited = inherited_dimensions(query, frozenset({Dimension.COUNTRY}))
        markup = wizard_confirm(inherited)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]

        assert "wizard:drop:language" in callbacks


# ---------------------------------------------------------------------------
# 5. Наскрізний сценарій із бага
# ---------------------------------------------------------------------------


class TestСценарійЗБага:
    """Повне відтворення того, що сталося в роботі."""

    def _apply(self, state: dict, text: str) -> dict:
        """Імітує обробку вільного запиту так само, як це робить бот."""
        parsed = parse_free_text(text, default_section=state.get("section_key", "magic"))
        if parsed.understood:
            state.update(query_to_state(parsed.query, parsed.mentioned))
        return state

    @pytest.mark.parametrize(
        "second_message",
        [
            "будь-яка країна, др від 50",
            "будь-яка країна др від 50",  # саме цей варіант і ламався
            "всі країни, DR від 50",
            "будь-яка країна, DR від 50",
        ],
    )
    def test_мова_більше_не_тягнеться(self, second_message):
        state: dict = {}
        self._apply(state, "англомовні донори")
        assert query_from_state(state).language is language_by_code("en")

        self._apply(state, second_message)
        final = query_from_state(state)

        assert final.language is None, "мова з попереднього запиту не має лишатися"
        assert final.dr_min == 50, "а DR має застосуватися"
        assert final.country is None

    def test_у_резюме_немає_чужої_мови(self):
        state: dict = {}
        self._apply(state, "англомовні донори")
        self._apply(state, "будь-яка країна др від 50")

        text = summary_lines(query_from_state(state), "Меджик", fresh_from_state(state))

        assert "англійська" not in text
        assert "Мова:</b> не обрано" in text
        assert INHERITED_MARK not in text

    def test_якщо_мову_не_чіпали_вона_лишається_але_підписана(self):
        """Пам'ять із ТЗ §29 працює — просто тепер вона видима.

        Другий запит нічого не каже про мову, тож у майстрі вона
        успадкується. Але буде підписана й прибереться одним дотиком.
        """
        state: dict = {}
        self._apply(state, "англомовні донори")

        # Користувач натиснув «Додати фільтр» — усе стає успадкованим.
        state[FRESH_KEY] = []
        query = query_from_state(state)

        text = summary_lines(query, "Меджик", fresh_from_state(state))
        assert "англійська" in text
        assert INHERITED_MARK in text
        assert "wizard:drop:language" in [
            b.callback_data
            for row in wizard_confirm(inherited_dimensions(query, frozenset())).inline_keyboard
            for b in row
        ]

    def test_після_скидання_фільтра_запит_чистий(self):
        state: dict = {}
        self._apply(state, "англомовні донори")
        state[FRESH_KEY] = []

        query = query_from_state(state).without(Dimension.LANGUAGE)
        state.update(query_to_state(query, frozenset()))

        assert query_from_state(state).language is None
