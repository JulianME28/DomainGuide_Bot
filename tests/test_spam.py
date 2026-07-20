"""Тести аналізу заспамленості для «Морд».

Заспамленість рахується у ВІДСОТКАХ: скільки з вихідних лінків заспамлені.
Абсолютне число без бази безглузде (10 заспамлених — це багато чи мало?),
тому працюємо з відсотком.

Ключові крайові випадки:
  * 0 вихідних лінків → відсоток невизначений, не ламає середні й фільтр;
  * порожні клітинки → не падаємо;
  * «Меджик» цих колонок не має — і не повинен показувати ці показники.
"""

from __future__ import annotations

import pytest

from app.analytics.engine import aggregate, run_query
from app.analytics.query import Dimension, DonorQuery
from app.data.models import Donor
from app.data.repository import build_donors
from app.text.cards import percent, render_result
from app.text.freeform import parse_free_text
from tests.fixtures.fake_data import (
    MORDY_AVG_OUTLINKS,
    MORDY_AVG_SPAM_PERCENT,
    MORDY_DONORS,
    MORDY_OUTLINKS_SAMPLE,
    MORDY_SPAM_SAMPLE,
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

    def test_нуль_вихідних_не_ламає_середні(self):
        """Донор із 0 вихідних не входить у середню заспамленість."""
        donors = [
            donor(outlinks=16, spammed=10),  # 62.5%
            donor(outlinks=0, spammed=0),  # невизначено — не рахується
            donor(outlinks=4, spammed=2),  # 50%
        ]
        result = aggregate(donors)

        assert result.spam_sample == 2, "у середню заспамленість пішли лише двоє"
        assert result.avg_spam_percent == 56.2, "середнє з 62.5 і 50, без нуля"
        assert result.count == 3, "але в загальній кількості всі троє"

    def test_нуль_вихідних_рахується_в_середню_вихідних(self):
        """0 вихідних — це реальне значення, у середню кількість воно входить."""
        donors = [donor(outlinks=16, spammed=10), donor(outlinks=0, spammed=0)]
        result = aggregate(donors)
        assert result.outlinks_sample == 2
        assert result.avg_outlinks == 8.0  # (16 + 0) / 2

    async def test_нуль_вихідних_не_у_фільтрі_заспамленості(self, mordy):
        """Фільтр по заспамленості відсіює донорів із 0 вихідних (%невизначений)."""
        result = run_query(mordy, mordy_query(spam_max=100))
        # m4 має 0 вихідних → не проходить, хоч 100 покриває будь-який відсоток.
        assert result.core.count == MORDY_SPAM_SAMPLE  # 5, без m4 і m6

    async def test_нуль_вихідних_лишається_в_загальній_кількості(self, mordy):
        """Без фільтра по заспамленості m4 (0 вихідних) присутній у базі."""
        result = run_query(mordy, mordy_query())
        assert result.core.count == MORDY_DONORS  # усі 7, зокрема m4


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

    async def test_середня_заспамленість(self, mordy):
        result = run_query(mordy, mordy_query())
        assert result.core.spam_sample == MORDY_SPAM_SAMPLE
        assert result.core.avg_spam_percent == MORDY_AVG_SPAM_PERCENT


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
        """До 30%: m2(25%), uk1(0%) = 2. Донори з невизначеним % не проходять."""
        result = run_query(mordy, mordy_query(spam_max=30))
        assert result.core.count == 2

    async def test_фільтр_заспамленості_мінімум(self, mordy):
        """Від 60%: m1(62.5%), glob(100%) = 2."""
        result = run_query(mordy, mordy_query(spam_min=60))
        assert result.core.count == 2

    async def test_комбінація_вихідних_і_заспамленості(self, mordy):
        """До 60 вихідних і заспамленість до 30%: m2(20,25%), uk1(10,0%) = 2."""
        result = run_query(mordy, mordy_query(outlinks_max=60, spam_max=30))
        assert result.core.count == 2

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
        assert "Середня заспамленість" in card
        assert "%" in card

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
        assert "Середня заспамленість" in card

    async def test_похибка_і_для_морд(self, mordy):
        card = render_result(run_query(mordy, mordy_query()))
        assert "до 30%" in card

    def test_невизначена_заспамленість_у_картці(self):
        """Якщо в усіх донорів 0 вихідних — чесно кажемо, що % не порахувати."""
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
        assert "Середня заспамленість:</b> —" in card
        assert "немає вихідних лінків" in card
