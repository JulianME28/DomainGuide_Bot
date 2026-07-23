"""Тести GEO як ОКРЕМОГО фільтра (колонка GEO — країна походження трафіку).

Головна відмінність від країнового запиту: «гео Польща» фільтрує по колонці
GEO (трафік із Польщі), НЕ по доменній зоні й НЕ по мові. А «Польща» без слова
«гео» лишається звичайним країновим запитом (трикроковий водоспад).
"""

from __future__ import annotations

from app.analytics.engine import run_query
from app.analytics.query import Dimension, DonorQuery
from app.bot.states import summary_lines
from app.data.models import Dataset, Donor
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from app.text.freeform import parse_free_text


def make_dataset(donors, *, tracks_geo=True):
    return Dataset("magic", "Меджик", "Меджик", tuple(donors), 0.0, tracks_geo=tracks_geo)


class TestРозборGEO:
    def test_гео_польща_це_фільтр_по_колонці(self):
        parsed = parse_free_text("донори Меджик, мова російська, гео Польща")
        assert parsed.query.geo is country_by_code("pl")
        assert parsed.query.language is language_by_code("ru")
        assert parsed.query.country is None, "«Польща» після «гео» — це не країновий запит"

    def test_польща_без_модифікатора_це_країновий_запит(self):
        parsed = parse_free_text("Польща")
        assert parsed.query.country is country_by_code("pl")
        assert parsed.query.geo is None

    def test_трафік_з_країни_це_теж_гео(self):
        assert parse_free_text("Меджик трафік з Польщі").query.geo is country_by_code("pl")

    def test_скасування_гео_працює(self):
        parsed = parse_free_text("гео не важливо, мова англійська")
        assert parsed.query.geo is None
        assert Dimension.GEO in parsed.cancelled
        assert parsed.query.language is language_by_code("en")


class TestФільтрГEO:
    def test_фільтрує_по_колонці_а_не_по_зоні(self):
        """Донор у зоні .pl без GEO=Польща НЕ проходить фільтр «гео Польща»."""
        zone_only = Donor("x.pl", ".pl", "polish", None, None)  # зона .pl, GEO немає
        geo_only = Donor("y.com", ".com", "english", None, None, geo_code="pl", geo_traffic=50)
        result = run_query(
            make_dataset([zone_only, geo_only]),
            DonorQuery(section_key="magic", geo=country_by_code("pl")),
        )
        assert result.core.count == 1  # лише geo_only, зона .pl не рахується

    def test_geo_нуль_не_проходить(self):
        """GEO=(pl, 0) — країна відома, трафіку не виміряно → фільтр не проходить."""
        measured = Donor("a.com", ".com", "english", None, None, geo_code="pl", geo_traffic=100)
        zero = Donor("b.com", ".com", "english", None, None, geo_code="pl", geo_traffic=0)
        result = run_query(
            make_dataset([measured, zero]),
            DonorQuery(section_key="magic", geo=country_by_code("pl")),
        )
        assert result.core.count == 1  # лише measured

    def test_комбінація_мова_і_гео(self):
        """«мова російська, гео Польща» = російською І з GEO Польща."""
        good = Donor("a.com", ".com", "russian", None, None, geo_code="pl", geo_traffic=100)
        wrong_geo = Donor("b.com", ".com", "russian", None, None, geo_code="de", geo_traffic=100)
        wrong_lang = Donor("c.com", ".com", "english", None, None, geo_code="pl", geo_traffic=100)
        result = run_query(
            make_dataset([good, wrong_geo, wrong_lang]),
            DonorQuery(
                section_key="magic",
                language=language_by_code("ru"),
                geo=country_by_code("pl"),
            ),
        )
        assert result.core.count == 1  # лише good

    def test_гео_ігнорується_для_бази_без_колонки(self):
        """База без GEO-колонки: фільтр знімається, а не відсіює всіх (хибний нуль)."""
        donors = [Donor("a.com", ".com", "english", None, None) for _ in range(3)]
        result = run_query(
            make_dataset(donors, tracks_geo=False),
            DonorQuery(section_key="magic", geo=country_by_code("pl")),
        )
        assert result.core.count == 3
        assert result.query.geo is None  # normalize_query зняв GEO


class TestПоказГEO:
    def test_у_рядку_запиту_видно_що_це_гео(self):
        query = DonorQuery(section_key="magic", geo=country_by_code("pl"))
        assert "гео Польща" in query.describe()

    def test_у_резюме_окремим_рядком(self):
        query = DonorQuery(section_key="magic", geo=country_by_code("pl"))
        text = summary_lines(query, "Меджик", tracks_geo=True)
        assert "Гео (країна трафіку)" in text and "Польща" in text

    def test_резюме_без_гео_колонки_рядка_немає(self):
        query = DonorQuery(section_key="magic")
        assert "Гео (країна трафіку)" not in summary_lines(query, "Меджик", tracks_geo=False)
