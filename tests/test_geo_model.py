"""Тести моделі країни — найважливіші в проєкті.

Країна рахується трикроковим водоспадом (зона → мова-на-GLOBAL → GEO) без
подвійного рахунку: кожен донор рівно в одній групі. Підсумок = сума трьох.
Останній рядок («мова на зонах інших країн») — окрема пропозиція, у підсумок
НЕ входить.
"""

from __future__ import annotations

import pytest

from app.analytics.engine import run_query
from app.analytics.query import DonorQuery, QueryKind
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from tests.fixtures.fake_data import (
    ENGLISH_LANGUAGE_TOTAL,
    FRANCE_ADDENDUM,
    FRANCE_GEO,
    FRANCE_LANG,
    FRANCE_TOTAL,
    FRANCE_ZONE,
    FRENCH_LANGUAGE_TOTAL,
    GERMAN_LANGUAGE_TOTAL,
    GERMANY_ADDENDUM,
    GERMANY_GEO,
    GERMANY_LANG,
    GERMANY_TOTAL,
    GERMANY_ZONE,
    UK_ADDENDUM,
    UK_GEO,
    UK_LANG,
    UK_TOTAL,
    UK_ZONE,
)


def country_query(code: str, **filters) -> DonorQuery:
    return DonorQuery(section_key="magic", country=country_by_code(code), **filters)


def language_query(code: str, **filters) -> DonorQuery:
    return DonorQuery(section_key="magic", language=language_by_code(code), **filters)


class TestТрикроковийПідсумок:
    async def test_франція(self, magic):
        result = run_query(magic, country_query("fr"))
        assert result.query.kind is QueryKind.COUNTRY
        assert result.split is not None
        assert (result.split.zone, result.split.language, result.split.geo) == (
            FRANCE_ZONE,
            FRANCE_LANG,
            FRANCE_GEO,
        )
        assert result.split.total == FRANCE_TOTAL == 5
        assert result.core.count == FRANCE_TOTAL

    async def test_німеччина(self, magic):
        result = run_query(magic, country_query("de"))
        assert (result.split.zone, result.split.language, result.split.geo) == (
            GERMANY_ZONE,
            GERMANY_LANG,
            GERMANY_GEO,
        )
        assert result.split.total == GERMANY_TOTAL == 9

    async def test_британія_дві_зони(self, magic):
        result = run_query(magic, country_query("gb"))
        assert (result.split.zone, result.split.language, result.split.geo) == (
            UK_ZONE,
            UK_LANG,
            UK_GEO,
        )
        assert result.split.total == UK_TOTAL == 5

    async def test_підсумок_це_сума_трьох(self, magic):
        for code in ("fr", "de", "gb"):
            result = run_query(magic, country_query(code))
            s = result.split
            assert s.total == s.zone + s.language + s.geo
            assert result.core.count == s.total, "core рахується по об'єднанню трьох груп"


class TestБезПодвійногоРахунку:
    async def test_кожен_донор_рівно_в_одній_групі(self, magic):
        """Сума трьох складових = кількість УНІКАЛЬНИХ донорів у підсумку."""
        from app.analytics.engine import classify_country

        zone_d, lang_d, geo_d, _ = classify_country(magic, country_query("de"))
        ids = [id(d) for d in zone_d] + [id(d) for d in lang_d] + [id(d) for d in geo_d]
        assert len(ids) == len(set(ids)), "жоден донор не потрапив у дві групи"
        assert len(ids) == GERMANY_TOTAL

    async def test_de6_зона_у_німеччині_geo_у_франції(self, magic):
        """Приклад із ТЗ: донор .de-зони з GEO (fr, 5000).

        Німеччина рахує його в ЗОНУ, Франція — у GEO. У кожному запиті — один раз.
        """
        from app.analytics.engine import classify_country

        de_zone, _, de_geo, _ = classify_country(magic, country_query("de"))
        assert any(d.domain == "de6.de" for d in de_zone), "у Німеччині — крок зони"
        assert not any(d.domain == "de6.de" for d in de_geo)

        _, _, fr_geo, _ = classify_country(magic, country_query("fr"))
        assert any(d.domain == "de6.de" for d in fr_geo), "у Франції — крок GEO"


class TestGEOКрок:
    async def test_geo_з_нулем_не_рахується(self, magic):
        """vn1 має GEO (fr, 0) — країна відома, трафіку немає → не в підсумку."""
        from app.analytics.engine import classify_country

        _, _, fr_geo, _ = classify_country(magic, country_query("fr"))
        assert not any(d.domain == "vn1.vn" for d in fr_geo)
        # France GEO = de6 + cn1 = 2, без vn1.
        assert len(fr_geo) == FRANCE_GEO == 2

    async def test_geo_дає_франції_двох(self, magic):
        result = run_query(magic, country_query("fr"))
        assert result.split.geo == 2  # de6(fr,5000) + cn1(fr,3000)

    async def test_зона_важливіша_за_geo(self, magic):
        """de1 має GEO (de, 1000), але зона .de — крок зони, не GEO."""
        from app.analytics.engine import classify_country

        de_zone, _, de_geo, _ = classify_country(magic, country_query("de"))
        assert any(d.domain == "de1.de" for d in de_zone)
        assert not any(d.domain == "de1.de" for d in de_geo)

    async def test_geo_показник_у_картці_лише_для_меджика(self, magic, mordy):
        assert run_query(magic, country_query("de")).split.show_geo is True
        # «Морди» колонки GEO не мають.
        assert (
            run_query(
                mordy, DonorQuery(section_key="mordy", country=country_by_code("de"))
            ).split.show_geo
            is False
        )


class TestМоваЛишеGLOBAL:
    async def test_мовний_крок_бере_лише_нейтральні_зони(self, magic):
        """Крок (б) — мова країни лише на GLOBAL_ZONES (.com/.net), не на .at/.ch."""
        from app.analytics.engine import classify_country

        _, de_lang, _, _ = classify_country(magic, country_query("de"))
        domains = {d.domain for d in de_lang}
        assert domains == {"glob1.com", "glob2.net"}, "лише німецька на нейтральних зонах"
        assert "at1.at" not in domains and "ch1.ch" not in domains

    async def test_німецька_на_at_ch_іде_в_додаток(self, magic):
        result = run_query(magic, country_query("de"))
        # at1(.at), ch1(.ch) — німецька на ccTLD інших країн → додаток, не підсумок.
        assert result.addendum.count == GERMANY_ADDENDUM == 2


class TestОстаннійРядок:
    async def test_франція_додаток(self, magic):
        result = run_query(magic, country_query("fr"))
        assert result.addendum is not None
        assert result.addendum.count == FRANCE_ADDENDUM == 1  # be1 на .be
        assert result.addendum.needs_warning is False

    async def test_британія_додаток(self, magic):
        result = run_query(magic, country_query("gb"))
        assert result.addendum.count == UK_ADDENDUM == 2  # de3(.de), fr3(.fr)
        assert result.addendum.needs_warning is True

    async def test_додаток_не_входить_у_підсумок(self, magic):
        """Головне: додаток окремий, у core.count його немає."""
        result = run_query(magic, country_query("de"))
        assert result.core.count == GERMANY_TOTAL
        assert result.addendum.count == GERMANY_ADDENDUM
        # Ці два числа не сумуються в підсумок.
        assert result.core.count == GERMANY_TOTAL  # не 9+2

    async def test_додаток_виключає_врахованих_у_підсумку(self, magic):
        """Донор, який зайшов у підсумок через GEO, у додаток НЕ потрапляє.

        de6 (French? ні — Turkish). Візьмемо перевірку інакше: усі донори
        додатка справді поза підсумком.
        """
        from app.analytics.engine import classify_country

        zone_d, lang_d, geo_d, add_d = classify_country(magic, country_query("gb"))
        in_total = {id(d) for d in zone_d + lang_d + geo_d}
        assert all(id(d) not in in_total for d in add_d)

    async def test_додаток_враховує_фільтри_метрик(self, magic):
        result = run_query(magic, country_query("de", dr_min=30))
        # зона .de dr≥30: de1(40),de4(55),de6(30)=3; мова: glob1(45)=1; geo: es1 dr21<30 =0 → 4
        assert result.split.total == 4
        assert result.split.zone == 3
        assert result.split.language == 1
        # додаток German на інших ccTLD dr≥30: at1(35)=1, ch1(20)✗
        assert result.addendum.count == 1

    async def test_якщо_додатка_немає_він_none(self, magic):
        result = run_query(magic, country_query("de", dr_min=100))
        assert result.core.count == 0
        assert result.addendum is None


class TestПопередженняПроСпільніМови:
    async def test_англійська_попереджає(self, magic):
        # Британія: у додатку англійська — спільна мова, попередження показуємо.
        assert run_query(magic, country_query("gb")).addendum.needs_warning is True

    @pytest.mark.parametrize("code", ["fr", "de"])
    async def test_однозначні_мови_не_попереджають(self, magic, code):
        assert run_query(magic, country_query(code)).addendum.needs_warning is False


class TestПохибкаВідПідсумку:
    async def test_нижня_межа_від_підсумку_а_не_складової(self, magic):
        """Похибка 30% рахується від 5 (підсумок Франції), не від 3 (зона)."""
        result = run_query(magic, country_query("fr"))
        assert result.core.count == 5
        assert result.core.min_estimate == 4  # (5×7+5)//10 = 4, не (3×7+5)//10=2

    async def test_німеччина_похибка(self, magic):
        result = run_query(magic, country_query("de"))
        assert result.core.count == 9
        assert result.core.min_estimate == 6  # від 9, не від 6 (зони)


class TestЗапитПроМову:
    async def test_мова_це_не_країна(self, magic):
        result = run_query(magic, language_query("de"))
        assert result.query.kind is QueryKind.LANGUAGE
        assert result.core.count == GERMAN_LANGUAGE_TOTAL  # 8 усіх німецькомовних
        assert result.split is None, "у запиту про мову немає розкладу країни"
        assert result.addendum is None

    async def test_французька_мова_загалом(self, magic):
        assert run_query(magic, language_query("fr")).core.count == FRENCH_LANGUAGE_TOTAL

    async def test_англійська_мова_загалом(self, magic):
        assert run_query(magic, language_query("en")).core.count == ENGLISH_LANGUAGE_TOTAL

    @pytest.mark.parametrize(("code", "expected"), [("zh", 1), ("vi", 1), ("tr", 2)])
    async def test_мови_розпізнаються_в_даних(self, magic, code, expected):
        assert run_query(magic, language_query(code)).core.count == expected


class TestКомбінованийЗапит:
    async def test_країна_і_мова_це_перетин(self, magic):
        query = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("de"),
        )
        result = run_query(magic, query)

        assert result.query.kind is QueryKind.COMBINED
        assert result.core.count == 4  # німецькомовні саме в зоні .de
        assert result.split is None, "комбінований запит не використовує водоспад"
        assert result.addendum is None


class TestГлобальніЗониНікомуНеНалежать:
    async def test_розподіл_по_країнах_відокремлює_глобальні(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        rows = dict(result.country_breakdown)
        assert rows["🌐 Глобальні зони (без країни)"] == 5

    async def test_глобальна_зона_не_стає_країною(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        labels = [label for label, _ in result.country_breakdown]
        assert not any("Колумбія" in label for label in labels)


class TestБазаБезGEO:
    async def test_морди_рахують_країну_без_geo(self, mordy):
        """«Морди» колонки GEO не мають — водоспад працює на двох кроках."""
        result = run_query(mordy, DonorQuery(section_key="mordy", country=country_by_code("de")))
        assert result.available
        assert result.split is not None
        assert result.split.geo == 0, "GEO-складова відсутня"
        assert result.split.show_geo is False
        # зона .de: m1, m4, m7 = 3
        assert result.split.zone == 3
        assert result.split.total == result.core.count

    async def test_биті_і_порожні_geo_не_падають(self):
        """GEO у брудному вигляді не має ламати підрахунок."""
        from app.analytics.engine import classify_country
        from app.data.models import Dataset, Donor

        donors = (
            Donor(domain="a.fr", zone=".fr", language="french", dr=None, traffic=None),
            Donor(
                domain="b.de",
                zone=".de",
                language="german",
                dr=None,
                traffic=None,
                geo_code="",
                geo_traffic=None,
            ),  # немає GEO
        )
        ds = Dataset(
            section_key="magic",
            title="Меджик",
            sheet_name="Меджик",
            donors=donors,
            loaded_at=0.0,
            tracks_geo=True,
        )
        zone_d, _lang_d, geo_d, _ = classify_country(ds, country_query("fr"))
        assert len(zone_d) == 1  # a.fr
        assert geo_d == []
