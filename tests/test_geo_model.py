"""Тести моделі гео — найважливіші в проєкті.

У даних немає колонки країни, тому країна виводиться з доменної зони та мови.
Це дає ДВА різні числа на одне питання, і головний ризик проєкту — тихо їх
переплутати або скласти. Тести нижче стережуть саме це.
"""

from __future__ import annotations

import pytest

from app.analytics.engine import run_query
from app.analytics.query import DonorQuery, QueryKind
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from tests.fixtures.fake_data import (
    ENGLISH_LANGUAGE_OUTSIDE_ZONE,
    ENGLISH_LANGUAGE_TOTAL,
    FRANCE_ZONE_COUNT,
    FRENCH_LANGUAGE_OUTSIDE_ZONE,
    FRENCH_LANGUAGE_TOTAL,
    GERMAN_LANGUAGE_OUTSIDE_ZONE,
    GERMAN_LANGUAGE_TOTAL,
    GERMANY_ZONE_COUNT,
    UK_ZONE_COUNT,
)


def country_query(code: str, **filters) -> DonorQuery:
    return DonorQuery(section_key="magic", country=country_by_code(code), **filters)


def language_query(code: str, **filters) -> DonorQuery:
    return DonorQuery(section_key="magic", language=language_by_code(code), **filters)


class TestДваЧислаНеСумуються:
    """Ядро моделі: зонове й мовне число — це різні множини."""

    async def test_німеччина_ядро_це_зона(self, magic):
        result = run_query(magic, country_query("de"))
        assert result.query.kind is QueryKind.COUNTRY
        assert result.core.count == GERMANY_ZONE_COUNT  # 6 донорів у зоні .de

    async def test_німеччина_мовний_додаток_окремо(self, magic):
        result = run_query(magic, country_query("de"))
        assert result.addendum is not None
        assert result.addendum.count == GERMAN_LANGUAGE_OUTSIDE_ZONE  # 4

    async def test_числа_не_складаються(self, magic):
        """Найголовніше твердження всього проєкту.

        У базі 6 донорів у зоні .de і 8 німецькомовних. Разом це НЕ 14:
        4 донори одночасно і в зоні, і німецькою мовою.
        """
        result = run_query(magic, country_query("de"))

        zone_count = result.core.count
        language_extra = result.addendum.count

        assert zone_count == 6
        assert language_extra == 4
        # Жодне з цих чисел не є сумою:
        assert zone_count + language_extra != GERMAN_LANGUAGE_TOTAL
        assert zone_count + language_extra == 10  # це число НІДЕ не показується

    async def test_мовний_додаток_виключає_зонових(self, magic):
        """Додаток = усі мовою мінус ті, хто вже в зоні. Без подвійного рахунку."""
        result = run_query(magic, country_query("de"))
        language_total = run_query(magic, language_query("de")).core.count

        assert language_total == GERMAN_LANGUAGE_TOTAL  # 8 усього німецькою
        donors_in_zone_and_language = language_total - result.addendum.count
        assert donors_in_zone_and_language == 4
        assert result.addendum.count == language_total - donors_in_zone_and_language

    async def test_запит_про_мову_має_ядром_мову(self, magic):
        """«німецькою» — це питання про мову, а не про Німеччину."""
        result = run_query(magic, language_query("de"))
        assert result.query.kind is QueryKind.LANGUAGE
        assert result.core.count == GERMAN_LANGUAGE_TOTAL  # 8, а не 6
        assert result.addendum is None, "для запиту про мову додатка немає"


class TestПопередженняПроСпільніМови:
    async def test_англійська_попереджає(self, magic):
        """en — спільна мова: англійською пишуть багато країн, не лише Британія."""
        result = run_query(magic, country_query("gb"))
        assert result.addendum is not None
        assert result.addendum.needs_warning is True

    async def test_французька_не_попереджає(self, magic):
        """fr — однозначна мова, попередження було б зайвим шумом."""
        result = run_query(magic, country_query("fr"))
        assert result.addendum is not None
        assert result.addendum.needs_warning is False

    @pytest.mark.parametrize("code", ["gb", "es"])
    async def test_спільні_мови(self, magic, code):
        assert run_query(magic, country_query(code)).addendum.needs_warning is True

    @pytest.mark.parametrize("code", ["fr", "de", "tr"])
    async def test_однозначні_мови(self, magic, code):
        assert run_query(magic, country_query(code)).addendum.needs_warning is False


class TestКраїниПоЗонах:
    async def test_франція(self, magic):
        result = run_query(magic, country_query("fr"))
        assert result.core.count == FRANCE_ZONE_COUNT  # 3 у зоні .fr
        assert result.addendum.count == FRENCH_LANGUAGE_OUTSIDE_ZONE  # 1 поза нею

    async def test_французька_мова_загалом(self, magic):
        assert run_query(magic, language_query("fr")).core.count == FRENCH_LANGUAGE_TOTAL

    async def test_британія_дві_зони(self, magic):
        """У Британії дві зони: .co.uk і .uk. Рахуються обидві."""
        result = run_query(magic, country_query("gb"))
        assert result.core.count == UK_ZONE_COUNT  # 3
        assert result.addendum.count == ENGLISH_LANGUAGE_OUTSIDE_ZONE  # 4

    async def test_англійська_мова_загалом(self, magic):
        assert run_query(magic, language_query("en")).core.count == ENGLISH_LANGUAGE_TOTAL

    async def test_туреччина_складена_зона(self, magic):
        """.com.tr має розпізнатися як Туреччина, а не як .tr чи глобальна зона."""
        result = run_query(magic, country_query("tr"))
        assert result.core.count == 1
        # У зоні .de є турецькомовний сайт — він потрапляє саме в мовний додаток.
        assert result.addendum.count == 1

    async def test_мова_і_зона_це_різні_речі(self, magic):
        """Турецькомовний сайт у зоні .de належить зоні Німеччини, а не Туреччини."""
        germany = run_query(magic, country_query("de"))
        turkey = run_query(magic, country_query("tr"))
        assert germany.core.count == 6  # турецькомовний de6.de рахується тут
        assert turkey.core.count == 1  # а тут його немає

    @pytest.mark.parametrize(("code", "expected"), [("zh", 1), ("vi", 1), ("tr", 2)])
    async def test_мови_розпізнаються_в_даних(self, magic, code, expected):
        """Turkish / Chinese / Vietnamese мають знаходитися в базі."""
        assert run_query(magic, language_query(code)).core.count == expected


class TestГлобальніЗониНікомуНеНалежать:
    async def test_глобальні_зони_не_потрапили_в_країни(self, magic):
        """Сума по всіх країнах не має включати донорів з .com, .net, .org."""
        result = run_query(magic, DonorQuery(section_key="magic"))
        rows = dict(result.country_breakdown)

        assert rows["🌐 Глобальні зони (без країни)"] == 5

    async def test_донор_на_com_не_рахується_жодній_країні(self, magic):
        """glob1.com німецькою мовою: у зону Німеччини не входить."""
        germany = run_query(magic, country_query("de"))
        # 6 донорів зони .de — жодного .com серед них.
        assert germany.core.count == 6
        # Але в мовний додаток він потрапляє: він німецькою і поза зоною .de.
        assert germany.addendum.count == 4

    async def test_розподіл_по_країнах_не_вигадує_гео(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        labels = [label for label, _ in result.country_breakdown]
        assert "🌐 Глобальні зони (без країни)" in labels
        # Жодна глобальна зона не має перетворитися на назву країни.
        assert not any("Колумбія" in label for label in labels)


class TestКомбінованийЗапит:
    async def test_країна_і_мова_разом(self, magic):
        """«німецькомовні донори в Німеччині» — це перетин, а не сума."""
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("de"),
        )
        result = run_query(magic, query)

        assert result.query.kind is QueryKind.COMBINED
        assert result.core.count == 4  # німецькомовні саме в зоні .de
        assert result.addendum is None, "користувач уже сам звузив запит"

    async def test_комбінація_вужча_за_кожну_умову_окремо(self, magic):
        combined = run_query(
            magic,
            DonorQuery(
                section_key="magic",
                country=country_by_code("de"),
                language=language_by_code("de"),
            ),
        ).core.count
        by_zone = run_query(magic, country_query("de")).core.count
        by_language = run_query(magic, language_query("de")).core.count

        assert combined <= by_zone
        assert combined <= by_language


class TestДодатокЗФільтрами:
    async def test_додаток_враховує_фільтри_метрик(self, magic):
        """Мовний додаток рахується за тими самими DR/трафіком, що й ядро.

        Інакше вийшло б нечесно: ядро відфільтроване, а додаток — ні.
        """
        result = run_query(magic, country_query("de", dr_min=30))

        # Ядро: у зоні .de з DR ≥ 30 → de1(40), de4(55), de6(30) = 3
        assert result.core.count == 3
        # Додаток: німецькою поза .de з DR ≥ 30 → at1(35), glob1(45) = 2
        assert result.addendum.count == 2

    async def test_якщо_додатка_немає_він_none(self, magic):
        """Якщо поза зоною немає нікого потрібною мовою — рядка не буде взагалі."""
        result = run_query(magic, country_query("de", dr_min=100))
        assert result.core.count == 0
        assert result.addendum is None
