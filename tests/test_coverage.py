"""Крок 2, Фаза A: операція coverage (покриття за потребою).

Ключове, що перевіряємо:
  * ЗАХИСТ needs↔traffic — потреба «треба 20 AU» НІКОЛИ не стає traffic_min=20
    (окреме поле "needs" + у CoverageQuery немає поля трафік-фільтра взагалі);
  * whitelist операції — та сама дисципліна, що й для полів (невідомий op/база,
    невідомі коди, позамежні числа, порожня потреба → операції немає);
  * рушій run_coverage рахує вердикт КОДОМ від найвищого порогу;
  * картка: рядок «Зрозумів як» перед числами; підсумок — з тих самих рядків.

ШІ тут не викликається: перевіряємо валідацію payload і детермінований рушій.
"""

from __future__ import annotations

import pytest

from app.analytics.coverage import CoverageVerdict, run_coverage
from app.analytics.query import CoverageQuery
from app.data.models import Dataset, Donor
from app.dictionary.countries import COUNTRIES, country_by_code
from app.llm.interpreter import (
    MAX_COVERAGE_COUNTRIES,
    MAX_NEED_PER_COUNTRY,
    MAX_THRESHOLDS,
    read_operation,
)


def _donor(zone: str, traffic: float) -> Donor:
    """Синтетичний донор: країна визначається зоною, мова/гео порожні."""
    return Donor(domain=f"x{traffic}{zone}", zone=zone, language="", dr=50.0, traffic=traffic)


def _mordy(donors: tuple[Donor, ...]) -> Dataset:
    return Dataset(
        section_key="mordy",
        title="Морди",
        sheet_name="m",
        donors=donors,
        loaded_at=0.0,
        tracks_spam=True,
    )


def _cov(section: str, needs: dict[str, int], thresholds: tuple[int, ...]) -> CoverageQuery:
    return CoverageQuery(
        section_key=section,
        needs=tuple((country_by_code(c), n) for c, n in needs.items()),
        thresholds=thresholds,
    )


# Payload запиту-2 рівно у тій формі, яку віддає реальна модель (смоук 8/8):
# потреба в "needs", пороги в "traffic_thresholds"; модель ще й дублює коди
# країн на верхньому рівні — вони НЕ мають нічого зіпсувати.
QUERY2_PAYLOAD = {
    "op": "coverage",
    "section": "mordy",
    "needs": {"au": 20, "ca": 12, "gb": 16, "nz": 8, "ie": 4},
    "traffic_thresholds": [20, 50],
    "au": 20,
    "ca": 12,
}


def _needs_map(op: CoverageQuery) -> dict[str, int]:
    return {country.code: amount for country, amount in op.needs}


class TestЗахистNeedsProтиTraffic:
    """Найважливіше: потреба не має просочитись у трафік ЖОДНИМ шляхом."""

    def test_треба_20_au_не_стає_traffic(self):
        op = read_operation(QUERY2_PAYLOAD)
        assert op is not None
        # Потреба осіла саме як потреба…
        assert _needs_map(op)["au"] == 20
        # …і в структурі coverage немає куди покласти трафік-фільтр:
        assert not hasattr(op, "traffic_min")
        assert not hasattr(op, "traffic_max")
        # Пороги — окремо, і 20/50 саме там (плюс 0 «всього»).
        assert set(op.thresholds) == {0, 20, 50}

    def test_сторонній_traffic_min_у_payload_ігнорується(self):
        # Навіть якщо модель помилково додасть traffic_min поряд із coverage —
        # операція його не читає (структурний захист, не лише промт).
        payload = {**QUERY2_PAYLOAD, "traffic_min": 20, "traffic_max": 999}
        op = read_operation(payload)
        assert op is not None
        assert _needs_map(op) == {"au": 20, "ca": 12, "gb": 16, "nz": 8, "ie": 4}
        assert set(op.thresholds) == {0, 20, 50}

    def test_потреба_читається_лише_з_needs(self):
        # Коди країн на верхньому рівні (au/ca) — НЕ потреба: беремо тільки needs.
        op = read_operation(QUERY2_PAYLOAD)
        assert _needs_map(op) == {"au": 20, "ca": 12, "gb": 16, "nz": 8, "ie": 4}


class TestWhitelistОперації:
    def test_повний_запит2(self):
        op = read_operation(QUERY2_PAYLOAD)
        assert isinstance(op, CoverageQuery)
        assert op.section_key == "mordy"
        assert [c.code for c, _ in op.needs] == ["au", "ca", "gb", "nz", "ie"]
        assert op.thresholds == (0, 20, 50)
        assert op.max_threshold == 50

    def test_невідомий_op_відкидається(self):
        assert read_operation({"op": "median", "section": "mordy", "needs": {"au": 5}}) is None

    def test_немає_op_це_не_операція(self):
        assert read_operation({"section": "mordy", "country": "au"}) is None

    def test_невідома_база_відкидається(self):
        assert read_operation({"op": "coverage", "section": "xxx", "needs": {"au": 5}}) is None

    def test_база_обовязкова(self):
        assert read_operation({"op": "coverage", "needs": {"au": 5}}) is None

    def test_порожня_потреба_після_валідації(self):
        # Усі коди невідомі → потреби не лишилось → операції немає (тихий фолбек).
        assert (
            read_operation({"op": "coverage", "section": "mordy", "needs": {"zz": 5, "qq": 3}})
            is None
        )

    def test_невідомі_коди_відкидаються_поштучно(self):
        op = read_operation(
            {"op": "coverage", "section": "mordy", "needs": {"au": 5, "zz": 9, "ca": 3}}
        )
        assert _needs_map(op) == {"au": 5, "ca": 3}

    def test_недодатна_й_позамежна_потреба_відкидається(self):
        op = read_operation(
            {
                "op": "coverage",
                "section": "mordy",
                "needs": {"au": 0, "ca": -5, "gb": MAX_NEED_PER_COUNTRY + 1, "nz": 7},
            }
        )
        assert _needs_map(op) == {"nz": 7}

    def test_кеп_кількості_країн(self):
        many = dict.fromkeys(COUNTRIES, 1)
        op = read_operation({"op": "coverage", "section": "mordy", "needs": many})
        assert len(op.needs) <= MAX_COVERAGE_COUNTRIES

    def test_поріг_0_завжди_присутній(self):
        op = read_operation({"op": "coverage", "section": "mordy", "needs": {"au": 5}})
        assert op.thresholds[0] == 0

    def test_кеп_порогів_зберігає_найвищі(self):
        op = read_operation(
            {
                "op": "coverage",
                "section": "mordy",
                "needs": {"au": 5},
                "traffic_thresholds": [10, 20, 30, 40, 50, 60, 70],
            }
        )
        assert len(op.thresholds) <= MAX_THRESHOLDS
        assert op.thresholds[0] == 0
        assert op.max_threshold == 70  # найвищий поріг збережено (він вирішує вердикт)

    def test_пороги_зростанням_без_повторів(self):
        op = read_operation(
            {
                "op": "coverage",
                "section": "mordy",
                "needs": {"au": 5},
                "traffic_thresholds": [50, 20, 20, 50],
            }
        )
        assert op.thresholds == (0, 20, 50)

    def test_дробові_й_рядкові_числа_приймаються(self):
        op = read_operation(
            {
                "op": "coverage",
                "section": "mordy",
                "needs": {"au": "20", "ca": 12.0},
                "traffic_thresholds": ["50"],
            }
        )
        assert _needs_map(op) == {"au": 20, "ca": 12}
        assert op.max_threshold == 50


# Синтетична база: AU з надлишком якісних; NZ — всього досить, якісних мало;
# IE — замало навіть усього. Пороги (0, 20, 50).
DONORS = (
    *(_donor(".au", 100.0) for _ in range(3)),  # AU: 3 донори, усі 50+
    *(_donor(".nz", 60.0) for _ in range(2)),  # NZ: 2 донори 50+
    *(_donor(".nz", 5.0) for _ in range(3)),  # NZ: ще 3 донори (лише «всього»)
    *(_donor(".ie", 100.0) for _ in range(2)),  # IE: 2 донори 50+
)


class TestРушійВердикт:
    def test_вистачає_навіть_на_найвищому_порозі(self):
        result = run_coverage(_mordy(DONORS), _cov("mordy", {"au": 2}, (0, 20, 50)))
        row = result.rows[0]
        assert row.total == 3
        assert row.top_count == 3
        assert row.verdict is CoverageVerdict.ENOUGH
        assert row.deficit == 0

    def test_всього_досить_але_якісних_бракує(self):
        result = run_coverage(_mordy(DONORS), _cov("mordy", {"nz": 4}, (0, 20, 50)))
        row = result.rows[0]
        assert row.total == 5  # усього вистачає (5 ≥ 4)
        assert row.top_count == 2  # на 50+ лише 2
        assert row.verdict is CoverageVerdict.LOW_QUALITY
        assert row.deficit == 2  # бракує 4 − 2, від НАЙВИЩОГО порогу

    def test_замало_навіть_усього(self):
        result = run_coverage(_mordy(DONORS), _cov("mordy", {"ie": 4}, (0, 20, 50)))
        row = result.rows[0]
        assert row.total == 2
        assert row.verdict is CoverageVerdict.SHORT
        assert row.deficit == 2  # бракує 4 − 2, від «всього» (поріг 0)

    def test_counts_за_кожним_порогом(self):
        result = run_coverage(_mordy(DONORS), _cov("mordy", {"nz": 4}, (0, 20, 50)))
        assert result.rows[0].counts == ((0, 5), (20, 2), (50, 2))

    def test_підсумки_з_тих_самих_рядків(self):
        result = run_coverage(
            _mordy(DONORS), _cov("mordy", {"au": 2, "nz": 4, "ie": 4}, (0, 20, 50))
        )
        # covered/short — це фільтр по тих самих rows (одне джерело правди).
        assert {r.country.code for r in result.covered} == {"au"}
        assert {r.country.code for r in result.short} == {"nz", "ie"}
        assert len(result.covered) + len(result.short) == len(result.rows)

    def test_порядок_країн_як_у_запиті(self):
        result = run_coverage(
            _mordy(DONORS), _cov("mordy", {"ie": 4, "au": 2, "nz": 4}, (0, 20, 50))
        )
        assert [r.country.code for r in result.rows] == ["ie", "au", "nz"]

    def test_недоступна_база(self):
        ds = Dataset(
            section_key="mordy",
            title="Морди",
            sheet_name="m",
            donors=(),
            loaded_at=0.0,
            available=False,
            error="мережа",
        )
        result = run_coverage(ds, _cov("mordy", {"au": 2}, (0, 20, 50)))
        assert not result.available
        assert result.rows == ()


class TestКартка:
    def test_зрозумів_як_перед_числами(self):
        from app.text.cards import render_coverage

        result = run_coverage(
            _mordy(DONORS), _cov("mordy", {"au": 2, "nz": 4, "ie": 4}, (0, 20, 50))
        )
        card = render_coverage(result)
        # Рядок «Зрозумів як» присутній і стоїть ПЕРЕД рядком покриття/числами.
        assert "Зрозумів як" in card
        assert "покриття по країнах" in card
        assert card.index("Зрозумів як") < card.index("покриття за потребою")
        # Потреба й пороги показані у шапці.
        assert "треба:" in card
        assert "пороги трафіку:" in card
        assert "всього" in card and "20+" in card and "50+" in card

    def test_вердикти_у_картці(self):
        from app.text.cards import render_coverage

        result = run_coverage(
            _mordy(DONORS), _cov("mordy", {"au": 2, "nz": 4, "ie": 4}, (0, 20, 50))
        )
        card = render_coverage(result)
        assert "✅" in card  # AU
        assert "⚠️" in card  # NZ
        assert "❌" in card  # IE
        assert "бракує 2" in card  # NZ дефіцит (4−2) і/або IE

    def test_підсумок_узгоджений_з_рядками(self):
        from app.text.cards import render_coverage

        result = run_coverage(
            _mordy(DONORS), _cov("mordy", {"au": 2, "nz": 4, "ie": 4}, (0, 20, 50))
        )
        card = render_coverage(result)
        # Підсумок каже 1/3 закрито — рівно стільки ✅-рядків.
        assert "Закрито 1/3" in card

    def test_усе_закрито(self):
        from app.text.cards import render_coverage

        result = run_coverage(_mordy(DONORS), _cov("mordy", {"au": 2}, (0, 20, 50)))
        card = render_coverage(result)
        assert "Потребу закрито по всіх" in card


# Різні формулювання ТІЄЇ САМОЇ потреби (запит-2) — для тригера wants_coverage.
_COVERAGE_PHRASINGS = [
    "морди, треба 20 AU 12 CA 16 UK 8 NZ 4 IE, скільки всього / 20+ трафік "
    "/ 50+ трафік, чого бракує",
    "по мордах потрібно: Австралія 20, Канада 12, Британія 16, Нова Зеландія 8, "
    "Ірландія 4. скільки всього, 20+, 50+, чого не вистачає",
    "Морди AU-20 CA-12 UK-16 NZ-8 IE-4. порахуй покриття по порогах трафіку 20 і 50",
    "морди план: 20 AU / 12 CA / 16 UK / 8 NZ / 4 IE. чи закриваємо потребу? 20+, 50+",
    "треба зібрати в мордах 20 AU 12 CA 16 UK 8 NZ 4 IE, скільки бракує на 20+ і 50+",
]


class TestТригерУСловнику:
    """Сигнал wants_coverage: словник має ПОМІТИТИ покривний запит (щоб хендлер
    віддав його ШІ), але НЕ перехоплювати звичайні мультикраїнні запити."""

    @pytest.mark.parametrize("text", _COVERAGE_PHRASINGS)
    def test_покривний_запит_має_сигнал(self, text):
        from app.text.freeform import parse_free_text

        parsed = parse_free_text(text)
        assert parsed.wants_coverage is True
        assert parsed.query.is_multi_country is True  # умова діверту в ШІ

    def test_звичайний_мультикраїнний_без_сигналу(self):
        # Регресія: перелік країн БЕЗ покривних слів не diverts у ШІ.
        from app.text.freeform import parse_free_text

        parsed = parse_free_text("меджик Британія Німеччина Франція трафік від 100")
        assert parsed.wants_coverage is False
        assert parsed.query.is_multi_country is True

    def test_одна_країна_з_словом_вистачає_не_кваліфікує_діверт(self):
        # «чи вистачає британії» — одна країна: діверт (умова is_multi_country)
        # не спрацює, навіть якщо сигнал є. Перевіряємо саме is_multi_country.
        from app.text.freeform import parse_free_text

        parsed = parse_free_text("меджик британія чи вистачає")
        assert parsed.query.is_multi_country is False
