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
    MORDY_DE_GEO,
    MORDY_DE_TOTAL,
    MORDY_DE_ZONE,
    SPAIN_NEUTRAL,
    SPAIN_TOTAL,
    UK_ADDENDUM,
    UK_GEO,
    UK_NEUTRAL,
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

    async def test_британія_спільна_мова_без_мовної_складової(self, magic):
        """Британія — англійська спільна: підсумок = зона + GEO, БЕЗ мови."""
        result = run_query(magic, country_query("gb"))
        assert result.split.show_language is False
        assert result.split.zone == UK_ZONE
        assert result.split.geo == UK_GEO
        assert result.split.total == UK_TOTAL == 3  # 5 донорів мовою НЕ додано
        assert result.core.count == UK_TOTAL

    async def test_підсумок_це_сума_складових(self, magic):
        for code in ("fr", "de", "gb"):
            result = run_query(magic, country_query(code))
            s = result.split
            expected = s.zone + s.geo + (s.language if s.show_language else 0)
            assert s.total == expected
            assert result.core.count == s.total, "core рахується по об'єднанню груп підсумку"


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

    async def test_geo_показник_у_обох_базах(self, magic, mordy):
        """Тепер GEO є і в «Меджику», і в «Мордах» — розклад показує GEO в обох."""
        assert run_query(magic, country_query("de")).split.show_geo is True
        mordy_de = run_query(mordy, DonorQuery(section_key="mordy", country=country_by_code("de")))
        assert mordy_de.split.show_geo is True


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


class TestСпільніМовиНеВПідсумку:
    """en/es/pt/ar: мова-на-нейтральних у підсумок НЕ входить (окремим рядком)."""

    async def test_британія_підсумок_без_мови(self, magic):
        result = run_query(magic, country_query("gb"))
        assert result.split.total == UK_TOTAL == 3  # зона 3 + GEO 0; мова НЕ додана
        assert result.split.show_language is False

    async def test_нейтральний_рядок_окремо_від_підсумку(self, magic):
        result = run_query(magic, country_query("gb"))
        assert result.neutral_offer is not None
        assert result.neutral_offer.count == UK_NEUTRAL == 2  # glob3.com, glob4.org
        assert result.core.count == UK_TOTAL, "нейтральні в підсумок не входять"

    async def test_нейтральний_рядок_без_подвійного_показу(self, magic):
        """Донори нейтрального рядка не перетинаються з підсумком (зона+GEO)."""
        from app.analytics.engine import classify_country

        zone_d, lang_global_d, geo_d, _ = classify_country(magic, country_query("gb"))
        in_total = {id(d) for d in zone_d + geo_d}  # для спільної мови це весь підсумок
        assert all(id(d) not in in_total for d in lang_global_d)

    async def test_geo_донор_іде_в_підсумок_а_не_в_нейтральний(self, magic):
        """.com-донор мовою з GEO країни → GEO (підсумок), не нейтральний рядок."""
        from app.analytics.engine import classify_country
        from app.data.models import Dataset, Donor

        donors = (Donor("x.com", ".com", "english", None, None, geo_code="gb", geo_traffic=500.0),)
        ds = Dataset("magic", "Меджик", "Меджик", donors, 0.0, tracks_geo=True)
        _zone_d, lang_global_d, geo_d, _ = classify_country(ds, country_query("gb"))
        assert len(geo_d) == 1, "GEO має пріоритет над мовою"
        assert lang_global_d == []

    async def test_іспанія_теж_спільна(self, magic):
        result = run_query(magic, country_query("es"))
        assert result.split.total == SPAIN_TOTAL == 1
        assert result.split.show_language is False
        assert result.neutral_offer.count == SPAIN_NEUTRAL == 1  # glob5.online

    async def test_однозначна_мова_без_нейтрального_рядка(self, magic):
        assert run_query(magic, country_query("de")).neutral_offer is None
        assert run_query(magic, country_query("fr")).neutral_offer is None


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


class TestМордиTeжMаютьGEO:
    async def test_морди_рахують_країну_трикроково(self, mordy):
        """«Морди» тепер мають GEO — той самий водоспад, що й у «Меджику»."""
        result = run_query(mordy, DonorQuery(section_key="mordy", country=country_by_code("de")))
        assert result.available
        assert result.split.show_geo is True
        assert result.split.zone == MORDY_DE_ZONE == 3  # m1, m4, m7
        assert result.split.geo == MORDY_DE_GEO == 1  # m2 (GEO de)
        assert result.split.total == MORDY_DE_TOTAL == 4
        assert result.core.count == MORDY_DE_TOTAL

    async def test_водоспад_без_подвійного_рахунку_в_мордах(self, mordy):
        """m1 має і зону .de, і GEO(de) — рахується РАЗ (зона важливіша)."""
        from app.analytics.engine import classify_country

        q = DonorQuery(section_key="mordy", country=country_by_code("de"))
        zone_d, lang_d, geo_d, _ = classify_country(mordy, q)
        ids = [id(d) for d in zone_d + lang_d + geo_d]
        assert len(ids) == len(set(ids)), "жоден донор не потрапив у дві групи"
        assert any(d.domain == "m1.de" for d in zone_d)
        assert not any(d.domain == "m1.de" for d in geo_d)

    async def test_морди_зберігають_вихідні_і_заспамленість(self, mordy):
        """GEO не витіснила аналіз заспамленості — середні лишилися."""
        result = run_query(mordy, DonorQuery(section_key="mordy", country=country_by_code("de")))
        assert result.tracks_spam is True
        assert result.core.avg_outlinks is not None

    async def test_биті_і_порожні_geo_в_мордах_не_падають(self, mordy):
        """m6 (порожня GEO) і m7 (битий формат) не ламають підрахунок."""
        m6 = next(d for d in mordy.donors if d.domain == "m6.pl")
        m7 = next(d for d in mordy.donors if d.domain == "m7.de")
        assert (m6.geo_code, m6.geo_traffic) == ("", None)
        assert (m7.geo_code, m7.geo_traffic) == ("", None)


class TestБазаБезGEO:
    """Синтетична база з tracks_geo=False — перевірка, що шлях без GEO не зламано."""

    def _dataset(self, tracks_geo: bool):
        from app.data.models import Dataset, Donor

        donors = (
            Donor("a.fr", ".fr", "french", None, None),
            Donor("b.be", ".be", "french", None, None),  # французька на .be
            Donor("c.com", ".com", "german", None, None),  # німецька на нейтральній
        )
        return Dataset("magic", "Меджик", "Меджик", donors, 0.0, tracks_geo=tracks_geo)

    async def test_без_geo_водоспад_на_двох_кроках(self):
        ds = self._dataset(tracks_geo=False)
        result = run_query(ds, country_query("fr"))
        assert result.split.show_geo is False
        assert result.split.geo == 0
        assert result.split.zone == 1  # a.fr
        assert result.split.total == result.core.count

    async def test_биті_і_порожні_geo_не_падають(self):
        from app.analytics.engine import classify_country

        ds = self._dataset(tracks_geo=True)
        zone_d, _lang_d, geo_d, _ = classify_country(ds, country_query("fr"))
        assert len(zone_d) == 1  # a.fr
        assert geo_d == []  # у синтетичних донорів GEO немає
