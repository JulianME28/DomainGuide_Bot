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

from app.analytics.engine import aggregate, run_query, spam_distribution
from app.analytics.query import Dimension, DonorQuery
from app.data.models import Donor
from app.data.repository import build_donors
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

    async def test_фільтр_заспамленості_у_кількості_бере_нуль(self, mordy):
        """Фільтр — у кількості: донор із 0 заспамлених проходить «до N».

        m4 (0 вихідних, 0 заспамлених) має spammed=0 ≤ 100 → проходить. Не
        проходить лише m6 з ПОРОЖНІМ значенням спаму (як донор без DR)."""
        result = run_query(mordy, mordy_query(spam_max=100))
        assert result.core.count == MORDY_DONORS - 1  # усі, крім m6 (порожній спам)

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
    async def test_фільтр_вихідних_максимум(self, mordy):
        """До 10 вихідних: m4(0), uk1(10), m7(8) = 3."""
        result = run_query(mordy, mordy_query(outlinks_max=10))
        assert result.core.count == 3

    async def test_фільтр_вихідних_нуль(self, mordy):
        """Рівно 0 вихідних: тільки m4."""
        result = run_query(mordy, mordy_query(outlinks_max=0))
        assert result.core.count == 1

    async def test_фільтр_заспамленості_максимум(self, mordy):
        """До 30 заспамлених: m1(10), m2(5), m4(0), uk1(0), m7(4) = 5.

        glob(52) відсіявся; m6 із порожнім значенням спаму не проходить."""
        result = run_query(mordy, mordy_query(spam_max=30))
        assert result.core.count == 5

    async def test_фільтр_заспамленості_мінімум(self, mordy):
        """Від 50 заспамлених: лише glob(52) = 1."""
        result = run_query(mordy, mordy_query(spam_min=50))
        assert result.core.count == 1

    async def test_комбінація_вихідних_і_заспамленості(self, mordy):
        """До 60 вихідних і до 30 заспамлених: m1,m2,m4,uk1,m7 = 5 (glob 52 — ні)."""
        result = run_query(mordy, mordy_query(outlinks_max=60, spam_max=30))
        assert result.core.count == 5

    async def test_фільтр_вихідних_на_меджику_ігнорується(self, magic):
        """У «Меджику» цих колонок немає — фільтр не має відсіяти геть усіх."""
        result = run_query(magic, DonorQuery(section_key="magic", outlinks_max=50))
        # normalize_query прибирає вимір, якого база не має → повна база.
        assert result.core.count == magic.count
        assert result.query.outlinks_max is None


# ---------------------------------------------------------------------------
# 7. Фрази скасування для нових вимірів
# ---------------------------------------------------------------------------


class TestФразиСкасування:
    @pytest.mark.parametrize(
        "text",
        [
            "будь-які вихідні лінки", "вихідних лінків будь-яка кількість",
            "без урахування вихідних лінків",
        ],
    )  # fmt: skip
    def test_скасування_вихідних(self, text):
        parsed = parse_free_text(text)
        assert Dimension.OUTLINKS in parsed.cancelled
        assert parsed.query.outlinks_min is None
        assert parsed.query.outlinks_max is None

    @pytest.mark.parametrize(
        "text",
        ["заспамленість не важлива", "будь-яка заспамленість", "без урахування заспамленості"],
    )
    def test_скасування_заспамленості(self, text):
        parsed = parse_free_text(text)
        assert Dimension.SPAM in parsed.cancelled
        assert parsed.query.spam_min is None
        assert parsed.query.spam_max is None

    def test_фільтр_вихідних_вільним_текстом(self):
        parsed = parse_free_text("Морди, вихідних лінків до 50")
        assert parsed.query.section_key == "mordy"
        assert parsed.query.outlinks_max == 50

    def test_скасування_вихідних_не_чіпає_трафік(self):
        """Той самий захист, що й для країни: скасування не з'їдає сусіда."""
        parsed = parse_free_text("будь-які вихідні лінки трафік від 100")
        assert parsed.query.traffic_min == 100
        assert parsed.query.outlinks_min is None


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
