"""Тести запиту по СПИСКУ країн в одному повідомленні.

Розподіл ЕКСКЛЮЗИВНИЙ: кожен донор дістається рівно ОДНІЙ країні за пріоритетом
водоспаду (зона > GEO > мова), при рівному — країні, раніше в запиті. Тому сума
по країнах = кількість донорів, без подвійного рахунку.
"""

from __future__ import annotations

import pytest

from app.analytics.engine import run_multi_country
from app.analytics.query import DonorQuery
from app.data.models import Dataset, Donor
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
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

    def test_список_з_uk_us_повний(self):
        """Реальний запит: 6 країн, і жодна (зокрема US) не губиться мовчки."""
        parsed = parse_free_text("UK, US, Канада, Австралія, Ірландія, Нова Зеландія")
        assert parsed.query.is_multi_country
        assert {c.code for c in parsed.query.countries} == {"gb", "us", "ca", "au", "ie", "nz"}
        assert parsed.unrecognized == ()  # нічого не втрачено й не зайве

    def test_названа_країна_не_зникає_або_в_не_впізнав(self):
        """Якщо назву не впізнали — вона в «не впізнав», а не зникає тихо."""
        parsed = parse_free_text("США, Канада, Ельдорадо")
        assert parsed.query.is_multi_country
        assert "ельдорадо" in parsed.unrecognized

    def test_список_кодів_вісім_країн(self):
        """Реальний баг: список із самих 2-літерних кодів — усі 8 країн."""
        parsed = parse_free_text("Морди по 8 країнах: UK, FR, DE, IT, ES, NL, BE, IE")
        assert parsed.query.is_multi_country
        assert {c.code for c in parsed.query.countries} == {
            "gb",
            "fr",
            "de",
            "it",
            "es",
            "nl",
            "be",
            "ie",
        }
        assert parsed.unrecognized == ()  # нічого не втрачено й не зайве
        assert parsed.query.section_key == "mordy"

    def test_код_нікому_не_належить_іде_в_не_впізнав(self):
        """2-літерний елемент, що не є країною, не зникає — він у «не впізнав»."""
        parsed = parse_free_text("UK, FR, XX, DE")
        assert {c.code for c in parsed.query.countries} == {"gb", "fr", "de"}
        assert "xx" in parsed.unrecognized

    def test_багато_країн_не_обрізаються(self):
        """Список нижче за максимум (30) не має тихо втрачати країн."""
        codes = "de, fr, it, es, nl, be, ie, pl, pt, se, at, ch, dk, no, fi"  # 15
        parsed = parse_free_text(codes)
        assert len(parsed.query.countries) == 15

    def test_разом_не_потрапляє_в_нерозпізнані(self):
        """«разом» — склеювач-підсумок, а не країна."""
        parsed = parse_free_text("Морди по США, Канаді та Австралії разом")
        assert {c.code for c in parsed.query.countries} == {"us", "ca", "au"}
        assert "разом" not in parsed.unrecognized
        assert parsed.unrecognized == ()

    def test_разом_далі_тригерить_підсумок(self):
        """При цьому «разом» лишається тригером підсумкового прапорця."""
        parsed = parse_free_text("Морди по США, Канаді та Австралії разом")
        assert parsed.want_total is True

    @pytest.mark.parametrize("glue", ["та", "і", "й", "по", "всього", "сумарно", "загалом"])
    def test_службові_слова_не_нерозпізнані(self, glue):
        parsed = parse_free_text(f"Німеччина {glue} Франція")
        assert parsed.query.is_multi_country
        assert glue not in parsed.unrecognized

    def test_опис_мовною_ознакою_у_рядку_запиту(self):
        """«англомовні країни» позначає ПРИНЦИП добору, не стає фільтром."""
        parsed = parse_free_text("всі англомовні країни: UK, US, Канада, Австралія, Ірландія")
        assert parsed.query.is_multi_country
        assert parsed.query.language is None  # не мовний фільтр
        assert "англомовних країн" in parsed.query.describe()

    def test_метричні_фільтри_на_весь_список(self):
        parsed = parse_free_text("Німеччина Франція трафік від 100")
        assert parsed.query.is_multi_country
        assert parsed.query.traffic_min == 100


def make_dataset(donors, *, tracks_geo=True):
    from app.data.models import Dataset

    return Dataset("magic", "Меджик", "Меджик", tuple(donors), 0.0, tracks_geo=tracks_geo)


class TestЕксклюзивнийРозподіл:
    def test_список_країн_поважає_or_фільтр_кількох_мов(self):
        dataset = Dataset(
            section_key="magic",
            title="Меджик",
            sheet_name="Меджик",
            donors=(
                Donor("en.com", ".com", "english", 30, 100, geo_code="gb", geo_traffic=10),
                Donor("de.com", ".com", "german", 30, 100, geo_code="de", geo_traffic=10),
                Donor("fr.com", ".com", "french", 30, 100, geo_code="fr", geo_traffic=10),
                Donor("es.com", ".com", "spanish", 30, 100, geo_code="es", geo_traffic=10),
            ),
            loaded_at=0,
            tracks_geo=True,
        )
        query = DonorQuery(
            section_key="magic",
            countries=tuple(
                country_by_code(code) for code in ("gb", "de", "fr", "es")
            ),
            languages=tuple(
                language_by_code(code) for code in ("en", "de", "fr")
            ),
            zones=(".com",),
        )

        result = run_multi_country(dataset, query)

        assert result.unique.count == 3

    async def test_розклад_ексклюзивний(self, magic):
        """Німецька — спільна (de/at/ch), тож мовний крок у підсумок не входить."""
        result = run_multi_country(magic, de_at_ch())
        by_code = {c.code: split for c, split in result.per_country}
        # Німеччина: зона .de(6) + GEO es1(1) = 7. glob1/glob2 (німецькою на .com)
        # не приписуються нікому — мова спільна.
        assert (by_code["de"].zone, by_code["de"].language, by_code["de"].geo) == (6, 0, 1)
        assert by_code["de"].total == 7
        assert by_code["de"].show_language is False
        assert by_code["at"].total == 1 and by_code["at"].zone == 1
        assert by_code["ch"].total == 1 and by_code["ch"].zone == 1

    async def test_сума_по_країнах_дорівнює_унікальним(self, magic):
        result = run_multi_country(magic, de_at_ch())
        total = sum(split.total for _c, split in result.per_country)
        assert total == result.unique.count == 9  # 6(зона de)+1(geo)+1(at)+1(ch)

    def test_зона_сильніша_за_geo(self):
        """.it із GEO(fr) при «Італія Франція» → Італія (зона > GEO)."""
        d = Donor("x.it", ".it", "italian", None, None, geo_code="fr", geo_traffic=500)
        query = DonorQuery(
            section_key="magic",
            countries=(country_by_code("it"), country_by_code("fr")),
        )
        by_code = {c.code: s for c, s in run_multi_country(make_dataset([d]), query).per_country}
        assert by_code["it"].zone == 1 and by_code["it"].total == 1
        assert by_code["fr"].geo == 0 and by_code["fr"].total == 0

    def test_спільномовний_донор_не_дістається_нікому(self):
        """Німецькою на .com — мова СПІЛЬНА, тож донор не в підсумку жодної країни
        (саме так зникає подвійний рахунок)."""
        d = Donor("g.com", ".com", "german", None, None)
        query = DonorQuery(
            section_key="magic", countries=(country_by_code("de"), country_by_code("at"))
        )
        result = run_multi_country(make_dataset([d]), query)
        by_code = {c.code: s for c, s in result.per_country}
        assert by_code["de"].total == 0 and by_code["at"].total == 0
        assert result.unique.count == 0

    async def test_сортування_за_спаданням(self, magic):
        query = DonorQuery(
            section_key="magic",
            countries=(country_by_code("ch"), country_by_code("de"), country_by_code("at")),
        )
        result = run_multi_country(magic, query)
        totals = [split.total for _c, split in result.per_country]
        assert totals == sorted(totals, reverse=True)
        assert result.per_country[0][0].code == "de"

    async def test_метрики_діють_на_всі_країни(self, magic):
        base = DonorQuery(
            section_key="magic", countries=(country_by_code("de"), country_by_code("fr"))
        )
        stricter = base.replace(dr_min=40)
        assert (
            run_multi_country(magic, stricter).unique.count
            <= run_multi_country(magic, base).unique.count
        )

    async def test_середні_по_набору(self, magic):
        result = run_multi_country(magic, de_at_ch())
        assert result.unique.avg_dr is not None
        assert result.unique.avg_traffic is not None

    async def test_одна_країна_у_списку_дорівнює_одно_країновому(self, magic):
        from app.analytics.engine import result_count

        one = DonorQuery(section_key="magic", countries=(country_by_code("de"),))
        multi = run_multi_country(magic, one)
        single = result_count(magic, DonorQuery(section_key="magic", country=country_by_code("de")))
        assert multi.per_country[0][1].total == single == 7
        assert multi.unique.count == single


class TestКарткаСписку:
    async def test_картка_має_розклад_у_дужках(self, magic):
        query = de_at_ch().replace(unrecognized=("атлантида",))
        card = render_multi_country(run_multi_country(magic, query, unrecognized=("атлантида",)))
        assert "Розклад по країнах" in card
        assert "(.de 6 | GEO 1)" in card  # німецька спільна — без складової «мова»
        assert "Разом донорів" in card
        assert "Кожен донор врахований лише в одній країні" in card
        assert "різниця через донорів" not in card  # рядок різниці прибрано
        assert "Не впізнав як країну" in card and "атлантида" in card

    async def test_у_картці_немає_доменів(self, magic):
        card = render_multi_country(run_multi_country(magic, de_at_ch()))
        for donor in magic.donors:
            assert donor.domain not in card

    async def test_немає_донорів_у_двох_країнах_одночасно(self, magic):
        """DE/AT/CH мають спільну (німецьку) мову — жоден донор не рахується двом.

        Перевіряємо, що ексклюзивний розподіл справді ексклюзивний: сума totals
        точно дорівнює кількості унікальних донорів."""
        result = run_multi_country(magic, de_at_ch())
        assert sum(s.total for _c, s in result.per_country) == result.unique.count


class TestСуміжніУСписку:
    async def test_суміжні_є_при_менше_7_країн(self, magic):
        from app.bot.execution import _compute_multi

        query = DonorQuery(
            section_key="magic", countries=(country_by_code("de"), country_by_code("fr"))
        )
        _result, suggestions = _compute_multi(magic, query)
        assert suggestions, "для короткого списку суміжні мають бути"
        # Суміжні не дублюють країни, що вже в запиті.
        labels = " ".join(s.label for s in suggestions)
        assert "Німеччина" not in labels and "Франція" not in labels

    async def test_суміжних_немає_при_7_і_більше(self, magic):
        from app.bot.execution import _compute_multi

        codes = ["de", "fr", "it", "es", "pl", "at", "ch"]  # 7 країн
        query = DonorQuery(section_key="magic", countries=tuple(country_by_code(c) for c in codes))
        _result, suggestions = _compute_multi(magic, query)
        assert suggestions == (), "для списку ≥7 країн суміжні не показуємо"
