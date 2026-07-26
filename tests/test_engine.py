"""Тести рушія: фільтри, середні, похибка, розподіли, стійкість, безпека."""

from __future__ import annotations

import pytest

from app.analytics.engine import (
    Aggregate,
    aggregate,
    cross_base_total,
    run_query,
    select_core,
)
from app.analytics.query import DonorQuery, QueryKind
from app.data.models import Dataset, Donor
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code


def donor(domain="x.de", zone=".de", language="german", dr=None, traffic=None) -> Donor:
    return Donor(domain=domain, zone=zone, language=language, dr=dr, traffic=traffic)


class TestСередні:
    def test_na_не_занижує_середній(self):
        """Донор без DR рахується в кількості, але не тягне середнє вниз."""
        donors = [donor(dr=40), donor(dr=None), donor(dr=20)]
        result = aggregate(donors)

        assert result.count == 3, "донор без DR усе одно існує"
        assert result.avg_dr == 30.0, "середнє з 40 і 20, а не з 40, 0 і 20"
        assert result.dr_sample == 2, "середнє порахували на двох донорах"

    def test_якщо_dr_немає_ні_в_кого(self):
        result = aggregate([donor(dr=None), donor(dr=None)])
        assert result.count == 2
        assert result.avg_dr is None

    def test_порожня_група(self):
        result = aggregate([])
        assert result.count == 0
        assert result.avg_dr is None
        assert result.is_empty

    def test_середні_округлюються(self):
        result = aggregate([donor(dr=10), donor(dr=11), donor(dr=13)])
        assert result.avg_dr == 11.3


class TestПохибка:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [(100, 70), (120, 84), (85, 60), (45, 32), (1, 1), (0, 0)],
    )
    def test_нижня_межа_це_кількість_на_07(self, count, expected):
        """Формула з ТЗ: мінімальна орієнтовна кількість = знайдено × 0.7."""
        assert Aggregate(count=count).min_estimate == expected


class TestПопередження:
    def test_мала_вибірка(self):
        """Середнє на двох донорах — ненадійне, треба попередити."""
        assert aggregate([donor(dr=40, traffic=100), donor(dr=20, traffic=50)]).low_sample

    def test_достатня_вибірка(self):
        donors = [donor(dr=40, traffic=100)] * 3
        assert not aggregate(donors).low_sample

    def test_коли_метрик_немає_зовсім(self):
        assert aggregate([donor(dr=None, traffic=None)]).low_sample

    def test_слабкі_показники(self):
        """ТЗ, розділ 7.6: середній DR або трафік менше 3 — це попередження."""
        donors = [donor(dr=1, traffic=500)] * 5
        assert aggregate(donors).weak_metrics

    def test_нормальні_показники(self):
        donors = [donor(dr=30, traffic=500)] * 5
        assert not aggregate(donors).weak_metrics


class TestФільтри:
    async def test_фільтр_dr_від(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic", dr_min=40))
        # DR ≥ 40: de1(40), de4(55), glob1(45), uk1(50), glob3(60), cn1(44) = 6
        assert result.core.count == 6

    async def test_фільтр_dr_до(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic", dr_max=10))
        # DR ≤ 10: shop.de5(10), glob4(5), glob5(8) = 3
        assert result.core.count == 3

    async def test_фільтр_трафіку(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic", traffic_min=1000))
        # ≥1000: de1(4800), de2(1200), de4(12000), glob1(3000), uk1(5000),
        #        glob3(10000), cn1(7000) = 7
        assert result.core.count == 7

    async def test_діапазон(self, magic):
        result = run_query(
            magic, DonorQuery(section_key="magic", dr_min=20, dr_max=30, traffic_min=100)
        )
        # DR 20..30 і трафік ≥100 (межі включно):
        #   de3(25,500), de6(30,200), ch1(20,700), fr1(30,400),
        #   fr2(22,150), be1(28,300), tr1(26,600), es1(21,320) = 8
        assert result.core.count == 8

    async def test_донор_без_dr_не_проходить_фільтр_по_dr(self, magic):
        """Якщо в таблиці стояло «n/a», ми не знаємо DR — значить, не гарантуємо."""
        with_filter = run_query(magic, DonorQuery(section_key="magic", dr_min=0)).core.count
        total = run_query(magic, DonorQuery(section_key="magic")).core.count

        assert total == 24
        assert with_filter == 23, "de2.de має DR «n/a» і у фільтр не потрапляє"

    async def test_комбінація_країни_і_метрик(self, magic):
        result = run_query(
            magic, DonorQuery(section_key="magic", country=country_by_code("de"), traffic_min=500)
        )
        # Німецька тепер СПІЛЬНА (основна для de/at/ch) — мовний крок НЕ в підсумку.
        # Підсумок з трафіком ≥500 = зона + GEO:
        #   зона .de: de1(4800),de2(1200),de3(500),de4(12000) = 4;  GEO: es1(320<500) = 0
        # glob1(3000) німецькою на .com у split.language рахується, але в підсумок не йде.
        assert result.core.count == 4
        assert (result.split.zone, result.split.language, result.split.geo) == (4, 1, 0)
        assert result.split.show_language is False

    async def test_запит_без_фільтрів_це_вся_база(self, magic):
        query = DonorQuery(section_key="magic")
        assert query.is_empty
        assert query.kind is QueryKind.METRICS
        assert run_query(magic, query).core.count == 24


class TestРозподіли:
    async def test_розподіл_по_зонах(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        zones = dict(result.zone_breakdown)
        assert zones[".de"] == 6
        assert zones[".fr"] == 3
        assert zones[".co.uk"] == 2

    async def test_розподіл_по_мовах(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        languages = dict(result.language_breakdown)
        assert languages["німецька"] == 8
        assert languages["англійська"] == 7

    async def test_розподіл_по_країнах(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        countries = dict(result.country_breakdown)
        assert countries["🇩🇪 Німеччина"] == 6
        assert countries["🇬🇧 Британія"] == 3, "дві зони .co.uk і .uk — це одна країна"

    async def test_розподіли_можна_вимкнути(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"), with_breakdowns=False)
        assert result.zone_breakdown == ()


class TestСтійкість:
    async def test_порожня_база_дає_нуль_без_падіння(self, repository):
        """«Морди» зараз порожні — це коректний нуль, а не помилка."""
        mordy = await repository.get("mordy")
        result = run_query(mordy, DonorQuery(section_key="mordy", country=country_by_code("de")))

        assert result.available
        assert result.core.count == 0
        assert result.core.min_estimate == 0
        assert result.addendum is None

    def test_недоступна_база_не_валить_запит(self):
        broken = Dataset(
            section_key="magic",
            title="Меджик",
            sheet_name="Меджик",
            donors=(),
            loaded_at=0.0,
            available=False,
            error="Google недоступний",
        )
        result = run_query(broken, DonorQuery(section_key="magic"))

        assert not result.available
        assert result.core.count == 0
        assert "Google недоступний" in result.error

    async def test_невідома_країна_дає_нуль_а_не_помилку(self, magic):
        """Країни немає в базі — це чесний нуль."""
        result = run_query(magic, DonorQuery(section_key="magic", country=country_by_code("jp")))
        assert result.core.count == 0

    def test_донори_без_зони_не_ламають_розподіл(self):
        dataset = Dataset(
            section_key="magic",
            title="Меджик",
            sheet_name="Меджик",
            donors=(donor(domain="дивне", zone="", language=""),),
            loaded_at=0.0,
        )
        result = run_query(dataset, DonorQuery(section_key="magic"))
        assert result.core.count == 1
        assert dict(result.country_breakdown)["❔ Зона не визначена"] == 1


class TestБезпекаДоменНеВитікає:
    """Домен — службове поле. Воно не має виходити з шару аналітики."""

    async def test_у_результаті_немає_жодного_домену(self, magic):
        """Найпряміша перевірка: беремо всі домени бази й шукаємо їх у результаті."""
        result = run_query(magic, DonorQuery(section_key="magic", country=country_by_code("de")))
        dumped = repr(result)

        for existing in magic.donors:
            assert existing.domain not in dumped, (
                f"домен {existing.domain} витік у результат запиту"
            )

    async def test_результат_не_містить_списку_донорів(self, magic):
        """Захист структурою: у QueryResult просто немає поля зі списком донорів."""
        result = run_query(magic, DonorQuery(section_key="magic"))

        for value in vars(result).values() if hasattr(result, "__dict__") else []:
            assert not isinstance(value, Donor)

        assert not hasattr(result, "donors")
        assert not hasattr(result.core, "donors")

    async def test_розподіли_містять_лише_зони_а_не_домени(self, magic):
        result = run_query(magic, DonorQuery(section_key="magic"))
        for label, _count in result.zone_breakdown:
            assert label.startswith("."), "у розподілі мають бути зони, а не домени"
            assert label.count(".") <= 2

    async def test_select_core_це_внутрішня_функція(self, magic):
        """Донорів можна дістати тільки навмисно, всередині шару аналітики."""
        donors = select_core(magic, DonorQuery(section_key="magic", country=country_by_code("de")))
        assert len(donors) == 6
        assert all(isinstance(d, Donor) for d in donors)


def _dataset(key: str, title: str, donors: tuple[Donor, ...]) -> Dataset:
    return Dataset(section_key=key, title=title, sheet_name=title, donors=donors, loaded_at=0.0)


class TestПідсумокПоБазах:
    """cross_base_total — унікальні домени по кількох базах, без подвійного рахунку."""

    def test_унікальні_домени_а_не_сума(self):
        """Спільний домен рахується РАЗ: разом менше, ніж проста сума."""
        magic = _dataset(
            "magic",
            "Меджик",
            (donor(domain="a.de"), donor(domain="b.de"), donor(domain="shared.de")),
        )
        mordy = _dataset("mordy", "Морди", (donor(domain="c.de"), donor(domain="shared.de")))
        query = DonorQuery(section_key="magic")

        total = cross_base_total([("Меджик", magic, query), ("Морди", mordy, query)])

        assert dict(total.per_base) == {"Меджик": 3, "Морди": 2}
        assert total.unique == 4, "a, b, c, shared — чотири унікальні, а не 3+2=5"
        assert total.overlap == 1, "shared.de є в обох базах"

    def test_без_перетину_overlap_нуль(self):
        """Якщо спільних доменів немає — overlap = 0, разом = проста сума."""
        magic = _dataset("magic", "Меджик", (donor(domain="a.de"), donor(domain="b.de")))
        mordy = _dataset("mordy", "Морди", (donor(domain="c.de"),))
        query = DonorQuery(section_key="magic")

        total = cross_base_total([("Меджик", magic, query), ("Морди", mordy, query)])

        assert total.unique == 3
        assert total.overlap == 0

    def test_повертає_лише_числа_без_доменів(self):
        """У структурі підсумку немає жодного домену — самі числа (безпека)."""
        magic = _dataset("magic", "Меджик", (donor(domain="secret1.de"),))
        mordy = _dataset("mordy", "Морди", (donor(domain="secret2.de"),))
        query = DonorQuery(section_key="magic")

        total = cross_base_total([("Меджик", magic, query), ("Морди", mordy, query)])

        assert "secret1.de" not in repr(total)
        assert "secret2.de" not in repr(total)


class TestОписЗапиту:
    def test_опис_країни(self):
        query = DonorQuery(section_key="magic", country=country_by_code("gb"), traffic_min=1)
        assert query.describe() == "Британія (.co.uk / .uk), трафік від 1, DR без обмеження"

    def test_опис_мови(self):
        query = DonorQuery(section_key="magic", language=language_by_code("fr"))
        assert "мова французька" in query.describe()

    def test_опис_діапазону(self):
        query = DonorQuery(section_key="magic", dr_min=20, dr_max=50)
        assert "DR від 20 до 50" in query.describe()

    def test_опис_порожнього_запиту(self):
        query = DonorQuery(section_key="magic")
        assert "без обмеження" in query.describe()

    def test_опис_списку_країн(self):
        query = DonorQuery(
            section_key="magic",
            countries=(country_by_code("de"), country_by_code("fr")),
        )
        assert query.describe().startswith("2 країн")

    def test_опис_списку_з_мовною_ознакою(self):
        query = DonorQuery(
            section_key="magic",
            countries=(country_by_code("gb"), country_by_code("us")),
            countries_note="англомовних",
        )
        assert query.describe().startswith("2 англомовних країн")

    def test_replace_робить_копію(self):
        original = DonorQuery(section_key="magic", dr_min=40)
        changed = original.replace(dr_min=30)

        assert original.dr_min == 40, "початковий запит не змінюється"
        assert changed.dr_min == 30
