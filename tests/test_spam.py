"""Тести аналізу заспамленості для «Морд».

Заспамленість рахується в АБСОЛЮТНІЙ КІЛЬКОСТІ заспамлених лінків:
  * у картці — РОЗПОДІЛ донорів за групами «0 / 1-20 / 21-50 / 51-100 / 100+»;
  * у фільтрі — «заспамленість до 40» = до 40 заспамлених лінків.

Ключові крайові випадки:
  * правило «0,0» (вихідних 0 і заспамлених 0) → група «100+» (непрацюючий сайт);
  * порожні клітинки → не падаємо й не потрапляють у групи;
  * «Меджик» цих колонок не має — і не повинен показувати ці показники.
"""

from __future__ import annotations

import pytest

from app.analytics.engine import aggregate, run_query, spam_beyond, spam_distribution
from app.analytics.query import Dimension, DonorQuery
from app.data.models import Dataset, Donor
from app.data.repository import build_donors
from app.dictionary.countries import country_by_code
from app.text.cards import percent, render_result
from app.text.freeform import parse_free_text
from tests.fixtures.fake_data import (
    MORDY_AVG_OUTLINKS,
    MORDY_DONORS,
    MORDY_OUTLINKS_SAMPLE,
    MORDY_OUTLINKS_ZEROS,
    MORDY_SPAM_DISTRIBUTION,
    magic_rows,
    mordy_rows,
)


def donor(outlinks=None, spammed=None, **kw) -> Donor:
    base = {"domain": "x.de", "zone": ".de", "language": "german", "dr": None, "traffic": None}
    base.update(kw)
    return Donor(outlinks=outlinks, spammed=spammed, **base)


def mordy_query(**filters) -> DonorQuery:
    return DonorQuery(section_key="mordy", **filters)


# ---------------------------------------------------------------------------
# 1. Колонки читаються з «Морд», відсутні в «Меджик»
# ---------------------------------------------------------------------------


class TestЧитанняКолонок:
    def test_морди_читають_вихідні_і_заспамлені(self):
        donors, _ = build_donors(mordy_rows())
        first = donors[0]  # m1: 16 вихідних, 10 заспамлених
        assert first.outlinks == 16
        assert first.spammed == 10

    def test_меджик_не_має_цих_колонок(self):
        """У «Меджику» немає ключів outlinks/spam — донори без цих даних."""
        donors, _ = build_donors(magic_rows())
        assert all(d.outlinks is None for d in donors)
        assert all(d.spammed is None for d in donors)

    async def test_морди_відстежують_заспамленість(self, mordy):
        assert mordy.tracks_spam
        assert mordy.count == MORDY_DONORS

    async def test_меджик_не_відстежує(self, magic):
        assert not magic.tracks_spam

    async def test_службова_колонка_raw_не_читається(self, spam_reader):
        """Навіть якщо в сирих даних буде "raw", бот її не візьме —
        у карті колонок такої ролі немає."""
        rows = [dict(r, raw="16, 10") for r in mordy_rows()]
        donors, _ = build_donors(rows)
        # raw не має жодного впливу: outlinks береться з колонки "Вихідні".
        assert donors[0].outlinks == 16
        assert not hasattr(donors[0], "raw")


# ---------------------------------------------------------------------------
# 2. Відсоток заспамленості рахується правильно
# ---------------------------------------------------------------------------


class TestВідсотокЗаспамленості:
    def test_приклад_із_тз(self):
        """10 з 16 = 62.5%, у картці показується як 63%."""
        d = donor(outlinks=16, spammed=10)
        assert d.spam_percent == 62.5
        assert percent(d.spam_percent) == "63%"

    @pytest.mark.parametrize(
        ("outlinks", "spammed", "expected"),
        [
            (20, 5, 25.0),
            (52, 52, 100.0),  # заспамлені всі
            (10, 0, 0.0),  # жодного заспамленого
            (8, 4, 50.0),
            (16, 10, 62.5),
        ],
    )
    def test_різні_пропорції(self, outlinks, spammed, expected):
        assert donor(outlinks=outlinks, spammed=spammed).spam_percent == expected

    def test_округлення_half_up(self):
        """62.5% має ставати 63%, а не 62 (як дав би банковий round)."""
        assert percent(62.5) == "63%"
        assert percent(0.0) == "0%"
        assert percent(None) == "—"


# ---------------------------------------------------------------------------
# 3. Нуль вихідних лінків — відсоток невизначений
# ---------------------------------------------------------------------------


class TestНульВихідних:
    def test_нуль_вихідних_дає_невизначений_відсоток(self):
        d = donor(outlinks=0, spammed=0)
        assert d.spam_percent is None, "0/0 — ділити не можна, відсоток невизначений"

    def test_нуль_нуль_іде_в_групу_сто_плюс(self):
        """Донор «0,0» (непрацюючий сайт) потрапляє в найгіршу групу «100+»."""
        donors = [
            donor(outlinks=16, spammed=10),  # 10 заспамлених → «1-20»
            donor(outlinks=0, spammed=0),  # правило «0,0» → «100+»
            donor(outlinks=4, spammed=2),  # 2 заспамлених → «1-20»
        ]
        result = aggregate(donors)

        assert result.spam_distribution == (("1-20", 2), ("100+", 1))
        assert result.count == 3, "усі троє — в загальній кількості"

    def test_нуль_вихідних_рахується_в_середню_вихідних(self):
        """0 вихідних — це реальне значення, у середню кількість воно входить."""
        donors = [donor(outlinks=16, spammed=10), donor(outlinks=0, spammed=0)]
        result = aggregate(donors)
        assert result.outlinks_sample == 2
        assert result.avg_outlinks == 8.0  # (16 + 0) / 2

    async def test_чистий_донор_проходить_а_мертвий_ні(self, mordy):
        """«До 100 заспамлених»: чистий uk1 (0 спаму, 10 вихідних) проходить, а
        мертвий m4 (0 вихідних) — ні. Не проходить і m6 (порожній спам)."""
        result = run_query(mordy, mordy_query(spam_max=100))
        # усі, крім m6 (порожній спам) і m4 (мертвий, 0 вихідних)
        assert result.core.count == MORDY_DONORS - 2

    async def test_нуль_вихідних_лишається_в_загальній_кількості(self, mordy):
        """Без фільтра по заспамленості m4 (0 вихідних) присутній у базі."""
        result = run_query(mordy, mordy_query())
        assert result.core.count == MORDY_DONORS  # усі 7, зокрема m4


# ---------------------------------------------------------------------------
# 3b. Розподіл за абсолютною кількістю заспамлених — головна зміна
# ---------------------------------------------------------------------------


class TestРозподілЗаспамленості:
    @pytest.mark.parametrize(
        ("spammed", "group"),
        [
            (0, "0"), (1, "1-20"), (20, "1-20"), (21, "21-50"), (50, "21-50"),
            (51, "51-100"), (100, "51-100"), (101, "100+"), (5000, "100+"),
        ],
    )  # fmt: skip
    def test_межі_груп(self, spammed, group):
        """Межі 0,1,20,21,50,51,100,101 мають лягати в правильну групу."""
        # outlinks>0 — щоб не спрацювало правило «0,0».
        dist = spam_distribution([donor(outlinks=5000, spammed=spammed)])
        assert dist == ((group, 1),)

    def test_нуль_нуль_іде_в_сто_плюс(self):
        assert spam_distribution([donor(outlinks=0, spammed=0)]) == (("100+", 1),)

    def test_нуль_заспамлених_з_вихідними_це_група_нуль(self):
        """0 заспамлених, але вихідні є — група «0», а не «100+»."""
        assert spam_distribution([donor(outlinks=10, spammed=0)]) == (("0", 1),)

    def test_порожні_не_потрапляють_у_групи(self):
        """Порожнє значення спаму (None) у жодну групу не входить і не падає."""
        donors = [donor(outlinks=10, spammed=None), donor(outlinks=None, spammed=None)]
        assert spam_distribution(donors) == ()

    def test_групи_з_нулем_донорів_не_показані(self):
        assert spam_distribution([donor(outlinks=200, spammed=5)]) == (("1-20", 1),)

    def test_нулі_окремо_від_порожніх(self):
        """0 у метриці рахується в *_zeros; порожнє (None) — ні."""
        result = aggregate(
            [
                donor(dr=0, traffic=0, outlinks=0, spammed=0),
                donor(dr=None, traffic=None, outlinks=None, spammed=None),
            ]
        )
        assert result.dr_zeros == 1
        assert result.traffic_zeros == 1
        assert result.outlinks_zeros == 1
        assert result.count == 2  # обидва — в загальній кількості


# ---------------------------------------------------------------------------
# 4. Порожні й биті значення — не падаємо
# ---------------------------------------------------------------------------


class TestСтійкість:
    def test_порожні_клітинки(self):
        d = donor(outlinks=None, spammed=None)
        assert d.spam_percent is None

    @pytest.mark.parametrize(
        ("outlinks_raw", "spam_raw"),
        [("", ""), ("n/a", "n/a"), ("-", "-"), ("абв", "хтось"), ("16", "")],
    )
    def test_биті_значення_не_падають(self, outlinks_raw, spam_raw):
        rows = [
            {
                "domain": "x.de", "language": "German", "dr": "10", "traffic": "10",
                "outlinks": outlinks_raw, "spam": spam_raw,
            }
        ]  # fmt: skip
        donors, _ = build_donors(rows)
        assert len(donors) == 1
        # Хоч що прийде — spam_percent або число, або None, але не виняток.
        assert donors[0].spam_percent is None or isinstance(donors[0].spam_percent, float)

    async def test_морди_з_даними_не_ламаються(self, mordy):
        result = run_query(mordy, mordy_query())
        assert result.available
        assert result.core.count == MORDY_DONORS


# ---------------------------------------------------------------------------
# 5. Середні по всій базі «Морди»
# ---------------------------------------------------------------------------


class TestСередніМорд:
    async def test_середня_кількість_вихідних(self, mordy):
        result = run_query(mordy, mordy_query())
        assert result.core.outlinks_sample == MORDY_OUTLINKS_SAMPLE
        assert result.core.avg_outlinks == MORDY_AVG_OUTLINKS

    async def test_розподіл_заспамленості(self, mordy):
        result = run_query(mordy, mordy_query())
        assert result.core.spam_distribution == MORDY_SPAM_DISTRIBUTION

    async def test_нулі_вихідних_поруч_із_середнім(self, mordy):
        """Біля середньої к-сті вихідних видно, скільки з них рівно 0."""
        result = run_query(mordy, mordy_query())
        assert result.core.outlinks_zeros == MORDY_OUTLINKS_ZEROS  # лише m4


# ---------------------------------------------------------------------------
# 6. Фільтри по нових вимірах
# ---------------------------------------------------------------------------


class TestФільтри:
    """Якість фільтрується ЛИШЕ по стовпцю G (заспамленість). F — службовий (F>0)."""

    async def test_фільтр_заспамленості_максимум(self, mordy):
        """До 30 заспамлених: m1(10), m2(5), uk1(0), m7(4) = 4.

        glob(52) відсіявся; мертвий m4(0,0) НЕ входить; m6 (порожній спам) — ні."""
        result = run_query(mordy, mordy_query(spam_max=30))
        assert result.core.count == 4

    async def test_фільтр_заспамленості_мінімум(self, mordy):
        """Від 50 заспамлених: лише glob(52) = 1."""
        result = run_query(mordy, mordy_query(spam_min=50))
        assert result.core.count == 1

    async def test_вихідні_і_заспамленість_дають_однаковий_результат(self, mordy):
        """«до 20 вихідних лінків» ≡ «заспамленість до 20» — обидва G ≤ 20."""
        by_outlinks = parse_free_text("Морди до 20 вихідних лінків").query
        by_spam = parse_free_text("Морди заспамленість до 20").query
        assert (by_outlinks.spam_min, by_outlinks.spam_max) == (None, 20)
        assert (by_spam.spam_min, by_spam.spam_max) == (None, 20)
        assert run_query(mordy, by_outlinks).core.count == run_query(mordy, by_spam).core.count

    async def test_F_ніколи_не_фільтрується_числом(self):
        """Хоч би що написали про «вихідні», числа в outlinks_* не потрапляють."""
        for text in ("Морди до 20 вихідних лінків", "Морди 5 вихідних", "Морди вихідних від 3"):
            query = parse_free_text(text).query
            assert not hasattr(query, "outlinks_min")
            assert not hasattr(query, "outlinks_max")

    async def test_мертвий_сайт_виключено_за_будь_якого_фільтра(self, mordy):
        """m4 (F=0, G=0) не входить ні в «до N», ні в «від N», ні в «незаспамлені»."""
        for query in (
            mordy_query(spam_max=100),  # ≤100 — усе, крім мертвого й порожнього
            mordy_query(spam_min=0),  # ≥0 — але мертвий усе одно ні (F=0)
            mordy_query(spam_max=0),  # незаспамлені = G=0 при F>0
        ):
            result = run_query(mordy, query)
            # Ідеально чистий uk1 (F=10, G=0) проходить, мертвий m4 — ні.
            assert result.core.count >= 1
        # Точна перевірка «незаспамлені»: лише uk1.
        assert run_query(mordy, mordy_query(spam_max=0)).core.count == 1

    def test_синтетичні_чистий_проходить_мертвий_ні(self):
        clean = donor(outlinks=10, spammed=0)
        dead = donor(outlinks=0, spammed=0)
        spammy = donor(outlinks=20, spammed=5)
        ds = Dataset("mordy", "Морди", "Морди", (clean, dead, spammy), 0.0, tracks_spam=True)
        # ≤3: чистий (G=0) проходить, spammy (G=5) — ні, мертвий (F=0) — ні.
        assert run_query(ds, mordy_query(spam_max=3)).core.count == 1

    async def test_фільтр_заспамленості_на_меджику_ігнорується(self, magic):
        """У «Меджику» цих колонок немає — фільтр не має відсіяти геть усіх."""
        result = run_query(magic, DonorQuery(section_key="magic", spam_max=50))
        # normalize_query прибирає вимір, якого база не має → повна база.
        assert result.core.count == magic.count
        assert result.query.spam_max is None


class TestПорядокМетрикаЗонаGEO:
    """Алгоритм: спершу метрика (G≤N при F>0), тоді країновий водоспад (зона, потім GEO)."""

    def _britain_dataset(self):
        gb_zone_ok = donor(outlinks=10, spammed=5, zone=".co.uk")  # зона + G ок
        gb_zone_bad = donor(outlinks=60, spammed=50, zone=".co.uk")  # зона, але G завелике
        gb_geo_ok = donor(outlinks=8, spammed=3, zone=".de", geo_code="gb", geo_traffic=500)
        gb_geo_bad = donor(outlinks=40, spammed=30, zone=".de", geo_code="gb", geo_traffic=500)
        donors = (gb_zone_ok, gb_zone_bad, gb_geo_ok, gb_geo_bad)
        return Dataset("mordy", "Морди", "Морди", donors, 0.0, tracks_spam=True, tracks_geo=True)

    def test_зонова_складова_рахується_по_G(self):
        """«Морди, Британія, до 20 вихідних»: у зоні лишається лише той, у кого G≤20."""
        ds = self._britain_dataset()
        query = DonorQuery(section_key="mordy", country=country_by_code("gb"), spam_max=20)
        result = run_query(ds, query)
        assert result.split.zone == 1  # gb_zone_ok; gb_zone_bad відсіявся по G
        assert result.split.geo == 1  # gb_geo_ok; gb_geo_bad відсіявся по G
        assert result.core.count == 2

    def test_вихідні_й_заспамленість_однакові_на_країні(self):
        """«до 20 вихідних» ≡ «заспамленість до 20» і в країновому запиті."""
        ds = self._britain_dataset()
        gb = country_by_code("gb")
        by_out = DonorQuery(section_key="mordy", country=gb, spam_max=20)
        assert run_query(ds, by_out).core.count == 2

    def test_без_фільтра_усі_чотири(self):
        """Без спам-фільтра — усі 4 британські донори (метрика нікого не ріже)."""
        ds = self._britain_dataset()
        query = DonorQuery(section_key="mordy", country=country_by_code("gb"))
        assert run_query(ds, query).core.count == 4


# ---------------------------------------------------------------------------
# 6b. «За порогом»: розподіл РЕШТИ при фільтрі заспамленості
# ---------------------------------------------------------------------------


def _britain_spread() -> Dataset:
    """Британія з розкидом заспамленості по всіх групах — для «за порогом».

    Усі в зоні .co.uk (зоновий крок Британії). У межах порога ≤20:
    2×G0 (група «0») і 3×G10 (група «1-20»). За порогом >20: 4×G30 (21-50),
    2×G60 (51-100), 1×G150 (100+). Плюс сторонній .de-донор — не Британія.
    """
    core = [donor(outlinks=50, spammed=0, zone=".co.uk") for _ in range(2)]
    core += [donor(outlinks=50, spammed=10, zone=".co.uk") for _ in range(3)]
    beyond = [donor(outlinks=50, spammed=30, zone=".co.uk") for _ in range(4)]
    beyond += [donor(outlinks=100, spammed=60, zone=".co.uk") for _ in range(2)]
    beyond += [donor(outlinks=200, spammed=150, zone=".co.uk") for _ in range(1)]
    other = [donor(outlinks=50, spammed=200, zone=".de")]  # не Британія
    donors = tuple(core + beyond + other)
    return Dataset("mordy", "Морди", "Морди", donors, 0.0, tracks_spam=True, tracks_geo=True)


class TestРозподілЗаПорогом:
    """spam_beyond: донори тієї ж країни, що ЗА порогом заспамленості (G > N)."""

    def _britain_query(self, **kw):
        return DonorQuery(section_key="mordy", country=country_by_code("gb"), **kw)

    def test_кількість_і_розподіл_решти(self):
        ds = _britain_spread()
        count, dist = spam_beyond(ds, self._britain_query(spam_max=20))
        assert count == 7  # 4 + 2 + 1
        assert dist == (("21-50", 4), ("51-100", 2), ("100+", 1))

    def test_чужа_країна_не_входить(self):
        """Сторонній .de-донор (G=200) у британську «решту» не потрапляє."""
        ds = _britain_spread()
        count, _ = spam_beyond(ds, self._britain_query(spam_max=20))
        assert count == 7  # без .de-донора було б 8

    def test_без_фільтра_решти_немає(self):
        ds = _britain_spread()
        assert spam_beyond(ds, self._britain_query()) == (0, ())

    def test_база_без_заспамленості_решти_немає(self, magic):
        assert spam_beyond(magic, DonorQuery(section_key="magic", spam_max=20)) == (0, ())

    @pytest.mark.parametrize(
        ("spam_max", "expect_core_group", "expect_beyond_first"),
        [
            (20, ("1-20", 3), ("21-50", 4)),  # межа 20|21
            (50, ("21-50", 4), ("51-100", 2)),  # межа 50|51
            (100, ("51-100", 2), ("100+", 1)),  # межа 100|101
        ],
    )  # fmt: skip
    def test_межа_ядро_решта(self, spam_max, expect_core_group, expect_beyond_first):
        """На кожній межі ядро (≤N) і решта (>N) рівно доповнюють одне одного."""
        ds = _britain_spread()
        core = run_query(ds, self._britain_query(spam_max=spam_max))
        assert expect_core_group in core.core.spam_distribution
        _, beyond = spam_beyond(ds, self._britain_query(spam_max=spam_max))
        assert beyond[0] == expect_beyond_first

    def test_немає_подвійного_рахунку(self):
        """Ядро (≤N) і решта (>N) не перетинаються: сума = уся Британія."""
        ds = _britain_spread()
        core = run_query(ds, self._britain_query(spam_max=20)).core.count
        beyond, _ = spam_beyond(ds, self._britain_query(spam_max=20))
        whole = run_query(ds, self._britain_query()).core.count
        assert core + beyond == whole


class TestКарткаЗаПорогом:
    """У картці рядок заспамленості ділиться на «в межах» і «за порогом»."""

    async def test_рядок_ділиться_на_межі_і_за_порогом(self):
        ds = _britain_spread()
        query = DonorQuery(section_key="mordy", country=country_by_code("gb"), spam_max=20)
        card = render_result(run_query(ds, query))
        assert "за порогом:" in card
        # У межах — групи «0» і «1-20»; за порогом — 21-50, 51-100, 100+.
        assert "2 (0)" in card and "3 (1-20)" in card
        assert "4 (21-50)" in card and "2 (51-100)" in card and "1 (100+)" in card

    async def test_нульові_групи_приховані_за_порогом(self):
        """Групи з нулем донорів у рядку «за порогом» не показуються."""
        # Лише одна група за порогом (21-50) — решти бути не має.
        core = [donor(outlinks=50, spammed=5, zone=".co.uk")]
        beyond = [donor(outlinks=50, spammed=30, zone=".co.uk")]
        ds = Dataset(
            "mordy", "Морди", "Морди", tuple(core + beyond), 0.0, tracks_spam=True, tracks_geo=True
        )
        query = DonorQuery(section_key="mordy", country=country_by_code("gb"), spam_max=20)
        card = render_result(run_query(ds, query))
        assert "за порогом:" in card
        assert "(51-100)" not in card and "(100+)" not in card

    async def test_без_фільтра_повний_розподіл_без_за_порогом(self, mordy):
        """Без спам-фільтра — рядок як раніше, без «за порогом»."""
        card = render_result(run_query(mordy, mordy_query()))
        assert "Заспамленість:" in card
        assert "за порогом:" not in card


# ---------------------------------------------------------------------------
# 7. Фрази скасування для нового виміру
# ---------------------------------------------------------------------------


class TestФразиСкасування:
    @pytest.mark.parametrize(
        "text",
        [
            "будь-які вихідні лінки", "вихідних лінків будь-яка кількість",
            "без урахування вихідних лінків",
            "заспамленість не важлива", "будь-яка заспамленість",
        ],
    )  # fmt: skip
    def test_скасування_якості_знімає_заспамленість(self, text):
        """І «вихідні», і «заспамленість» ведуть на скасування виміру SPAM (G)."""
        parsed = parse_free_text(text)
        assert Dimension.SPAM in parsed.cancelled
        assert parsed.query.spam_min is None
        assert parsed.query.spam_max is None

    def test_фільтр_вихідних_вільним_текстом_це_заспамленість(self):
        parsed = parse_free_text("Морди, вихідних лінків до 50")
        assert parsed.query.section_key == "mordy"
        assert parsed.query.spam_max == 50

    def test_скасування_вихідних_не_чіпає_трафік(self):
        """Той самий захист, що й для країни: скасування не з'їдає сусіда."""
        parsed = parse_free_text("будь-які вихідні лінки трафік від 100")
        assert parsed.query.traffic_min == 100
        assert parsed.query.spam_min is None


# ---------------------------------------------------------------------------
# 8. Картка результату
# ---------------------------------------------------------------------------


class TestКартка:
    async def test_картка_морд_має_нові_поля(self, mordy):
        card = render_result(run_query(mordy, mordy_query()))
        assert "вихідних лінків" in card
        # Заспамленість тепер РОЗПОДІЛ за кількістю, а не середній відсоток.
        assert "Заспамленість:" in card
        assert "Середня заспамленість" not in card
        # Групи розподілу видно в картці (напр. «3 (1-20)»).
        assert "(1-20)" in card

    async def test_картка_меджика_без_нових_полів(self, magic):
        """Формат картки «Меджика» не змінюється — заспамленості там немає."""
        from app.dictionary.countries import country_by_code

        card = render_result(
            run_query(magic, DonorQuery(section_key="magic", country=country_by_code("de")))
        )
        assert "вихідних лінків" not in card
        assert "заспамленість" not in card.lower()

    async def test_показ_у_форматі_dr_і_трафіку(self, mordy):
        """Нові рядки поруч із DR і трафіком, у тому самому стилі."""
        card = render_result(run_query(mordy, mordy_query()))
        assert "Середній DR" in card
        assert "Середній трафік" in card
        assert "Середня к-сть вихідних лінків" in card
        assert "Заспамленість:" in card

    async def test_нулі_показані_біля_середнього(self, mordy):
        """Біля середньої к-сті вихідних — приписка про нулі (m4 = 1 нуль)."""
        card = render_result(run_query(mordy, mordy_query()))
        assert "(з яких =0 — 1)" in card

    async def test_похибка_і_для_морд(self, mordy):
        card = render_result(run_query(mordy, mordy_query()))
        # Новий однорядковий формат похибки — однаковий для всіх баз.
        assert "допустима похибка 30%" in card
        assert "з урахуванням похибки" in card

    def test_нуль_нуль_у_картці_показаний_як_сто_плюс(self):
        """Донори «0,0» за правилом непрацюючого сайту йдуть у групу «100+»."""
        from app.data.models import Dataset

        dataset = Dataset(
            section_key="mordy",
            title="Морди",
            sheet_name="Морди",
            donors=(donor(outlinks=0, spammed=0), donor(outlinks=0, spammed=0)),
            loaded_at=0.0,
            tracks_spam=True,
        )
        card = render_result(run_query(dataset, DonorQuery(section_key="mordy")))
        assert "2 (100+)" in card
        assert "(з яких =0 — 2)" in card  # обидва мають 0 вихідних
