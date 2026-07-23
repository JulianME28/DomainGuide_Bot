"""Тести запиту по СПИСКУ країн в одному повідомленні.

Головна пастка — подвійний рахунок: країни зі спільною мовою (Німеччина/
Австрія/Швейцарія) ділять тих самих донорів на нейтральних зонах, тож проста
сума завищена. «Разом унікальних» має рахувати кожного донора РАЗ.
"""

from __future__ import annotations

import re

import pytest

from app.analytics.engine import run_multi_country
from app.analytics.query import DonorQuery
from app.dictionary.countries import country_by_code
from app.text.cards import render_multi_country
from app.text.freeform import parse_free_text


def de_at_ch() -> DonorQuery:
    return DonorQuery(
        section_key="magic",
        countries=(country_by_code("de"), country_by_code("at"), country_by_code("ch")),
    )


class TestПарсингСписку:
    @pytest.mark.parametrize(
        "text",
        [
            "меджик Німеччина Австрія Швейцарія",  # пробіли, українською
            "меджик Германия, Австрия, Швейцария",  # коми, російською
            "меджик Німеччина\nАвстрія\nШвейцарія",  # стовпчиком
            "меджик Германия Австрія Швейцария",  # мови впереміш
        ],
    )
    def test_список_парситься_різними_роздільниками(self, text):
        parsed = parse_free_text(text)
        assert parsed.query.is_multi_country
        assert {c.code for c in parsed.query.countries} == {"de", "at", "ch"}

    def test_російські_українські_і_зона_впереміш(self):
        parsed = parse_free_text("Франція Индия .de")
        assert {c.code for c in parsed.query.countries} == {"fr", "in", "de"}

    def test_одна_країна_не_список(self):
        """Рівно одна країна — звичайний одно-країновий запит, як раніше."""
        parsed = parse_free_text("меджик Німеччина")
        assert not parsed.query.is_multi_country
        assert parsed.query.country is country_by_code("de")

    def test_нерозпізнані_перелічуються(self):
        parsed = parse_free_text("Німеччина Франція Атлантида Мордор")
        assert parsed.query.is_multi_country
        assert "атлантида" in parsed.unrecognized
        assert "мордор" in parsed.unrecognized

    def test_метричні_фільтри_на_весь_список(self):
        parsed = parse_free_text("Німеччина Франція трафік від 100")
        assert parsed.query.is_multi_country
        assert parsed.query.traffic_min == 100


class TestПідрахунокСписку:
    async def test_розклад_правильний(self, magic):
        result = run_multi_country(magic, de_at_ch())
        counts = {c.code: n for c, n in result.per_country}
        # Німеччина: зона .de(6) + мова glob1,glob2(2) + GEO es1(1) = 9.
        # Австрія/Швейцарія: своя зона(1) + ті самі glob1,glob2(2) = 3.
        assert counts == {"de": 9, "at": 3, "ch": 3}
        assert result.sum_counts == 15

    async def test_унікальний_підсумок_без_подвійного_рахунку(self, magic):
        result = run_multi_country(magic, de_at_ch())
        # glob1, glob2 належать усім трьом країнам, але в унікальних — раз.
        assert result.unique.count == 11
        assert result.unique.count <= result.sum_counts
        assert result.has_overlap, "сума 15 > унікальних 11 — є перетин"

    async def test_сортування_за_спаданням(self, magic):
        # Подаємо навмисно не по порядку — має відсортувати за кількістю.
        query = DonorQuery(
            section_key="magic",
            countries=(country_by_code("ch"), country_by_code("de"), country_by_code("at")),
        )
        result = run_multi_country(magic, query)
        counts = [n for _c, n in result.per_country]
        assert counts == sorted(counts, reverse=True)
        assert result.per_country[0][0].code == "de"

    async def test_метрики_діють_на_всі_країни(self, magic):
        base = DonorQuery(
            section_key="magic",
            countries=(country_by_code("de"), country_by_code("fr")),
        )
        stricter = base.replace(dr_min=40)
        assert (
            run_multi_country(magic, stricter).unique.count
            <= run_multi_country(magic, base).unique.count
        )

    async def test_середні_по_унікальному_набору(self, magic):
        result = run_multi_country(magic, de_at_ch())
        assert result.unique.avg_dr is not None
        assert result.unique.avg_traffic is not None

    async def test_одна_країна_у_списку_дорівнює_одно_країновому(self, magic):
        """Список з однієї країни рахує стільки ж, скільки одно-країновий запит."""
        from app.analytics.engine import result_count

        one = DonorQuery(section_key="magic", countries=(country_by_code("de"),))
        multi = run_multi_country(magic, one)
        single = result_count(magic, DonorQuery(section_key="magic", country=country_by_code("de")))
        assert multi.per_country[0][1] == single == 9
        assert multi.unique.count == single


class TestКарткаСписку:
    async def test_картка_має_розклад_і_підсумок(self, magic):
        query = de_at_ch().replace(unrecognized=("атлантида",))
        card = render_multi_country(run_multi_country(magic, query, unrecognized=("атлантида",)))
        assert "Розклад по країнах" in card
        assert "Разом унікальних донорів" in card
        assert "різниця через донорів" in card  # є перетин
        assert "Не впізнав як країну" in card and "атлантида" in card

    async def test_у_картці_немає_доменів(self, magic):
        card = render_multi_country(run_multi_country(magic, de_at_ch()))
        for donor in magic.donors:
            assert donor.domain not in card

    async def test_без_перетину_рядка_різниці_немає(self, magic):
        """Франція і Британія не ділять донорів → сума = унікальних."""
        query = DonorQuery(
            section_key="magic",
            countries=(country_by_code("fr"), country_by_code("gb")),
        )
        result = run_multi_country(magic, query)
        assert result.sum_counts == result.unique.count
        assert not result.has_overlap
        assert "різниця через донорів" not in re.sub("<[^>]+>", "", render_multi_country(result))
