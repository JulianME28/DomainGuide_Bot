"""Тести режиму «ТІЛЬКИ ДОМЕННА ЗОНА».

Країновий запит рахує водоспадом: зона + GEO + мова. Іноді потрібно саме
перше — донори з доменом у цій зоні, і нічого більше. Для цього є модифікатор
«у зоні X»: без водоспаду, без GEO і без мови.

Назва країни БЕЗ модифікатора («Британія») й далі означає країновий запит.
"""

from __future__ import annotations

import pytest

from app.analytics.engine import run_query
from app.analytics.query import Dimension, DonorQuery, QueryKind
from app.dictionary.countries import country_by_code
from app.text.cards import render_result
from app.text.freeform import parse_free_text


def zone_query(text: str) -> DonorQuery:
    return parse_free_text(text).query


class TestРозборЗони:
    @pytest.mark.parametrize(
        "text",
        [
            "зона .co.uk",
            "у зоні .co.uk",
            "в зоні .co.uk",
            "доменна зона .co.uk",
            "тільки зона .co.uk",
            "лише зона .co.uk",
            "Скільки донорів Меджик у зоні .co.uk?",
        ],
    )
    def test_модифікатори_дають_режим_зони(self, text):
        query = zone_query(text)
        assert query.kind is QueryKind.ZONE
        assert query.country is None, "це запит по зоні, а не по країні"
        assert query.zones == (".co.uk", ".uk")

    def test_назва_країни_після_модифікатора(self):
        """«зона Британія» = «зона .co.uk» — обидві ccTLD країни."""
        assert zone_query("зона Британія").zones == zone_query("зона .co.uk").zones

    def test_кілька_ccTLD_охоплені_всі(self):
        assert zone_query("зона Туреччина").zones == (".com.tr", ".tr")
        assert zone_query("тільки зона .com.tr").zones == (".com.tr", ".tr")

    def test_країна_без_модифікатора_лишається_країновою(self):
        parsed = parse_free_text("Британія")
        assert parsed.query.country is country_by_code("gb")
        assert parsed.query.zones == ()
        assert parsed.query.kind is QueryKind.COUNTRY

    @pytest.mark.parametrize("text", ["зона не важлива", "будь-яка зона"])
    def test_фрази_скасування(self, text):
        parsed = parse_free_text(text)
        assert parsed.query.zones == ()
        assert Dimension.ZONE in parsed.cancelled

    def test_скасування_не_чіпає_сусідній_фільтр(self):
        parsed = parse_free_text("будь-яка зона, DR від 50")
        assert parsed.query.zones == ()
        assert parsed.query.dr_min == 50


class TestКомбінаціїЗони:
    def test_зона_і_dr(self):
        query = zone_query("зона .co.uk, DR від 20")
        assert query.zones == (".co.uk", ".uk")
        assert query.dr_min == 20

    def test_зона_і_трафік(self):
        query = zone_query("зона .de з трафіком від 50")
        assert query.zones == (".de",)
        assert query.traffic_min == 50

    def test_зона_і_мова(self):
        query = zone_query("зона .de, мова англійська")
        assert query.zones == (".de",)
        assert query.language.code == "en"

    def test_зона_і_гео(self):
        query = zone_query("зона .fr, гео Німеччина")
        assert query.zones == (".fr",)
        assert query.geo is country_by_code("de")


class TestПідрахунокЗони:
    async def test_рахує_лише_зону_без_geo_і_мови(self, magic):
        """Німеччина країновим запитом = 9 (зона 6 + мова 2 + GEO 1), зоною = 6."""
        country = run_query(magic, DonorQuery(section_key="magic", country=country_by_code("de")))
        assert country.core.count == 9
        assert country.split.zone == 6

        zone = run_query(magic, zone_query("зона .de"))
        assert zone.core.count == 6, "лише домени в зоні .de, без GEO і мови"

    async def test_зона_країни_дорівнює_зоновій_складовій(self, magic):
        """Те, що дає кнопка «Тільки доменна зона», = зонова складова картки."""
        britain = country_by_code("gb")
        country = run_query(magic, DonorQuery(section_key="magic", country=britain))
        zone = run_query(magic, DonorQuery(section_key="magic", zones=tuple(britain.zones)))
        assert zone.core.count == country.split.zone

    async def test_зона_з_фільтром(self, magic):
        """DR≥20 у зоні .de: de1(40), de3(25), de4(55), de6(30) = 4."""
        result = run_query(magic, zone_query("зона .de, DR від 20"))
        assert result.core.count == 4


class TestКарткаЗони:
    async def test_без_розкладу_в_дужках(self, magic):
        result = run_query(magic, zone_query("зона .de"))
        card = render_result(result)
        found = next(line for line in card.split("\n") if "Знайдено донорів" in line)
        assert "(" not in found, "у режимі зони складова одна — дужок бути не має"
        assert "6" in found

    async def test_у_рядку_запиту_видно_зону(self, magic):
        card = render_result(run_query(magic, zone_query("у зоні .co.uk")))
        assert "зона .co.uk / .uk" in card

    async def test_середні_й_похибка_як_завжди(self, magic):
        card = render_result(run_query(magic, zone_query("зона .de")))
        assert "Середній DR" in card
        assert "допустима похибка 30%" in card


class TestКнопкаТількиЗона:
    def test_кнопка_є_лише_для_країнового_запиту(self):
        from app.bot.keyboards import result_menu

        def codes(markup):
            return [b.callback_data for row in markup.inline_keyboard for b in row]

        assert "res:zoneonly" in codes(result_menu("magic", has_country=True))
        assert "res:zoneonly" not in codes(result_menu("magic", has_country=False))

    async def test_кнопка_дає_зонову_складову(self, magic):
        """Перезапуск того самого запиту в режимі зони дає зонове число."""
        query = DonorQuery(section_key="magic", country=country_by_code("de"), dr_min=20)
        country = run_query(magic, query)

        # Саме це робить обробник res:zoneonly.
        zone = run_query(magic, query.replace(country=None, zones=tuple(query.country.zones)))
        assert zone.core.count == country.split.zone
