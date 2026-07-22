"""Рекомендації: що запропонувати, коли донорів замало.

Це та частина, заради якої бот і потрібен у продажах. Клієнт просить
донорів по Німеччині, їх 6 — і замість «більше немає» бот одразу показує,
де взяти ще: сусідні німецькомовні країни, м'якше знижені вимоги, запасна
група.

П'ять сценаріїв:

  1. СУМІЖНІ КРАЇНИ ЗІ СПІЛЬНОЮ МОВОЮ
     Німеччина → Австрія (.at), Швейцарія (.ch). Список береться зі
     словника автоматично: це всі країни, де та сама основна мова.

  2. СУМІЖНІ КРАЇНИ РЕГІОНУ
     Франція → інші європейські гео (ТЗ, розділ 13.1).

  3. ПОНИЖЕННЯ ВИМОГ
     DR мінус 10, трафік навпіл. Показуємо, скільки донорів це додасть.

  4. ЯДРО + ЗАПАС
     Основна група (точно за запитом) і запасна (ширша). Ці два числа
     МОЖНА складати: запасна група навмисно будується так, щоб не
     перетинатися з основною. Це принципова відмінність від зонового
     й мовного чисел, які не сумуються ніколи.

  5. АНАЛІЗ ДЕФІЦИТУ
     Який саме фільтр найбільше ріже вибірку. Перевіряємо просто:
     по черзі прибираємо кожен фільтр і дивимось, де приріст найбільший.

Як і решта аналітики, цей модуль віддає ЛИШЕ числа — доменів тут немає.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analytics.engine import (
    normalize_query,
    passes_metrics,
    passes_result,
    result_count,
)
from app.analytics.query import DonorQuery, QueryKind
from app.data.models import Dataset
from app.dictionary.countries import countries_in_region, countries_with_language
from app.dictionary.zones import is_global_zone

# Наскільки пом'якшуємо вимоги в підказках.
DR_RELAXATION = 10  # DR: поріг на ±10
TRAFFIC_DIVIDER = 2  # трафік і вихідні лінки: поріг ÷2 (min) або ×2 (max)
SPAM_RELAXATION = 10  # заспамленість: поріг на ±10 відсоткових пунктів

# Скільки суміжних гео показувати, щоб не завалити користувача списком.
MAX_SUGGESTIONS = 6


@dataclass(frozen=True, slots=True)
class Suggestion:
    """Один варіант «а ще можна взяти ось це»."""

    label: str
    count: int
    query: DonorQuery | None = None
    """Готовий запит — щоб кнопка одразу могла його виконати."""


@dataclass(frozen=True, slots=True)
class ReserveGroup:
    """Ядро (точно за запитом) і запас (ширше)."""

    core_count: int
    reserve_count: int
    reserve_label: str

    @property
    def total(self) -> int:
        """Загальний потенціал.

        Тут складати МОЖНА: запас будується як «те, чого немає в ядрі»,
        тому подвійного рахунку не буде.
        """
        return self.core_count + self.reserve_count


@dataclass(frozen=True, slots=True)
class DeficitHint:
    """Який фільтр найбільше обмежує вибірку."""

    filter_label: str
    current_count: int
    without_filter_count: int

    @property
    def gain(self) -> int:
        return self.without_filter_count - self.current_count


@dataclass(frozen=True, slots=True)
class Recommendations:
    """Усі підказки для одного результату."""

    same_language: tuple[Suggestion, ...] = ()
    same_region: tuple[Suggestion, ...] = ()
    relaxed: tuple[Suggestion, ...] = ()
    reserve: ReserveGroup | None = None
    deficit: DeficitHint | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.same_language or self.same_region or self.relaxed or self.reserve or self.deficit
        )

    @property
    def extra_total(self) -> int:
        """Скільки додаткових донорів дають суміжні гео разом."""
        return sum(s.count for s in self.same_language) + sum(s.count for s in self.same_region)


def _count(dataset: Dataset, query: DonorQuery) -> int:
    """Скільки донорів у підсумку запиту (трикрокова модель для країни)."""
    return result_count(dataset, query)


def _country_totals(dataset: Dataset, query: DonorQuery, countries: tuple) -> dict[str, int]:
    """Трикроковий підсумок для КОЖНОЇ країни-кандидата — за ОДИН прохід.

    Навіщо саме так. Суміжних країн до п'яти-шести, а в базі ~31 000 рядків.
    Окремий прохід на кожну країну — це під двісті тисяч зайвих перевірок і
    відчутна пауза. Один зовнішній прохід по донорах із коротким внутрішнім
    циклом по кандидатах дає ті самі числа миттєво (як зроблено у 747dfb6).

    Метрики (DR/трафік) поточного запиту застосовуються; змінна тут — країна.
    """
    # Для кожної країни: зони, ключі мови і чи мова спільна (тоді крок «мова»
    # у підсумок не входить — саме заради цього й уся зміна).
    specs = [
        (
            c.code,
            frozenset(c.zones),
            c.language.data_keys if c.language else frozenset(),
            bool(c.language and c.language.widespread),
        )
        for c in countries
    ]
    counts = {c.code: 0 for c in countries}

    for donor in dataset.donors:
        if not passes_metrics(donor, query):
            continue
        zone = donor.zone
        language = donor.language
        geo_code = donor.geo_code
        geo_ok = donor.has_measured_geo
        is_global = is_global_zone(zone)
        for code, zones, keys, widespread in specs:
            # Те саме правило, що й у моделі країни: зона + GEO завжди;
            # мова на нейтральних зонах — лише для однозначних мов.
            if (
                zone in zones
                or (geo_ok and geo_code == code)
                or (not widespread and language in keys and is_global)
            ):
                counts[code] += 1

    return counts


def _country_suggestions(
    candidates: tuple, counts: dict[str, int], query: DonorQuery
) -> tuple[Suggestion, ...]:
    """Перетворює список країн-кандидатів на підказки з кількостями."""
    suggestions: list[Suggestion] = []

    for country in candidates:
        count = counts.get(country.code, 0)
        if count:
            suggestions.append(
                Suggestion(
                    label=f"{country.flag} {country.name_uk} ({country.main_zone})",
                    count=count,
                    query=query.replace(country=country),
                )
            )

    suggestions.sort(key=lambda s: s.count, reverse=True)
    return tuple(suggestions[:MAX_SUGGESTIONS])


# ---------------------------------------------------------------------------
# 1-2. Суміжні країни
# ---------------------------------------------------------------------------


def same_language_suggestions(dataset: Dataset, query: DonorQuery) -> tuple[Suggestion, ...]:
    """Країни з тією самою основною мовою: Німеччина → Австрія, Швейцарія."""
    country = query.country
    if country is None:
        return ()

    candidates = countries_with_language(country.primary_language, exclude=country.code)
    return _country_suggestions(candidates, _country_totals(dataset, query, candidates), query)


def same_region_suggestions(dataset: Dataset, query: DonorQuery) -> tuple[Suggestion, ...]:
    """Суміжні гео того самого регіону (ТЗ, розділ 13.1)."""
    country = query.country
    if country is None:
        return ()

    # Країни зі спільною мовою показуються окремим блоком — тут їх не дублюємо.
    already = {c.code for c in countries_with_language(country.primary_language)}
    candidates = tuple(
        neighbour
        for neighbour in countries_in_region(country.region, exclude=country.code)
        if neighbour.code not in already
    )
    return _country_suggestions(candidates, _country_totals(dataset, query, candidates), query)


# ---------------------------------------------------------------------------
# 3. Пониження вимог
# ---------------------------------------------------------------------------


def relaxed_suggestions(dataset: Dataset, query: DonorQuery) -> tuple[Suggestion, ...]:
    """М'якше знижені вимоги: DR мінус 10, трафік навпіл.

    Показуємо лише ті варіанти, які реально дають БІЛЬШЕ донорів.
    """
    if not query.has_metric_filters:
        return ()

    current = _count(dataset, query)
    variants: list[tuple[str, DonorQuery]] = []

    if query.dr_min:
        softer_dr = max(0.0, query.dr_min - DR_RELAXATION)
        variants.append((f"DR від {_int(softer_dr)}", query.replace(dr_min=softer_dr)))

    if query.traffic_min:
        softer_traffic = query.traffic_min / TRAFFIC_DIVIDER
        variants.append(
            (f"трафік від {_int(softer_traffic)}", query.replace(traffic_min=softer_traffic))
        )

    if query.dr_min and query.traffic_min:
        both = query.replace(
            dr_min=max(0.0, query.dr_min - DR_RELAXATION),
            traffic_min=query.traffic_min / TRAFFIC_DIVIDER,
        )
        variants.append((f"DR від {_int(both.dr_min)} і трафік від {_int(both.traffic_min)}", both))

    suggestions = []
    for label, variant in variants:
        count = _count(dataset, variant)
        if count > current:
            suggestions.append(Suggestion(label=label, count=count, query=variant))

    return tuple(suggestions)


# ---------------------------------------------------------------------------
# 4. Ядро + запас
# ---------------------------------------------------------------------------


def _relax_metrics(query: DonorQuery) -> tuple[DonorQuery, list[str]]:
    """Будує запит зі зниженими метричними порогами й описує, що саме знижено.

    Послаблення РОЗШИРЮЄ діапазон: нижній поріг («від») опускаємо, верхній
    («до») піднімаємо. DR — на 10, трафік і вихідні лінки — удвічі,
    заспамленість — на 10 відсоткових пунктів.

    Повертає (пом'якшений запит, список фраз «метрика від/до X замість Y»).
    Порожній список означає, що послаблювати не було чого — і рядка запасу
    тоді не буде.
    """
    changes: dict[str, float] = {}
    labels: list[str] = []

    def loosen_min(field: str, name: str, new: float) -> None:
        old = getattr(query, field)
        if old and new < old:
            changes[field] = new
            labels.append(f"{name} від {_int(new)} замість {_int(old)}")

    def loosen_max(field: str, name: str, new: float) -> None:
        old = getattr(query, field)
        if old is not None and new > old:
            changes[field] = new
            labels.append(f"{name} до {_int(new)} замість {_int(old)}")

    loosen_min("dr_min", "DR", max(0.0, (query.dr_min or 0) - DR_RELAXATION))
    loosen_max("dr_max", "DR", (query.dr_max or 0) + DR_RELAXATION)
    loosen_min("traffic_min", "трафік", (query.traffic_min or 0) / TRAFFIC_DIVIDER)
    loosen_max("traffic_max", "трафік", (query.traffic_max or 0) * TRAFFIC_DIVIDER)
    loosen_min("outlinks_min", "вихідні лінки", (query.outlinks_min or 0) / TRAFFIC_DIVIDER)
    loosen_max("outlinks_max", "вихідні лінки", (query.outlinks_max or 0) * TRAFFIC_DIVIDER)
    loosen_min("spam_min", "заспамленість", max(0.0, (query.spam_min or 0) - SPAM_RELAXATION))
    loosen_max("spam_max", "заспамленість", min(100.0, (query.spam_max or 0) + SPAM_RELAXATION))

    return query.replace(**changes), labels


def reserve_group(dataset: Dataset, query: DonorQuery) -> ReserveGroup | None:
    """Ядро + запас: скільки донорів додасться, ЯКЩО послабити метрики.

    Запас має сенс лише коли є що послаблювати. Якщо метричних фільтрів немає
    (DR і трафік без обмежень, для «Морд» — і вихідні лінки із заспамленістю),
    рядок «Ядро + запас» не показуємо взагалі.

    Запас = ПРИРІСТ від послаблення: донори тієї самої країни (за тією ж
    трикроковою логікою, що й підсумок), які НЕ проходять вихідні пороги, але
    проходять знижені. Формула passes_result(softer) AND NOT passes_result(query)
    сама собою виключає:
      * ядро — воно проходить і вихідні пороги;
      * мовні рядки «на зонах інших країн» / «на нейтральних зонах» — їхні
        донори взагалі не в підсумку країни, тож passes_result для них хибний.
    Тому «Разом» можна назвати клієнту без подвійного рахунку.
    """
    country = query.country
    if country is None or query.kind is not QueryKind.COUNTRY:
        return None

    softer, relaxations = _relax_metrics(query)
    if not relaxations:
        return None  # жодного метричного порогу — послаблювати нічого

    reserve_count = sum(
        1
        for donor in dataset.donors
        if passes_result(donor, softer) and not passes_result(donor, query)
    )
    if reserve_count == 0:
        return None

    return ReserveGroup(
        core_count=result_count(dataset, query),
        reserve_count=reserve_count,
        reserve_label="з пониженими вимогами (" + ", ".join(relaxations) + ")",
    )


# ---------------------------------------------------------------------------
# 5. Аналіз дефіциту
# ---------------------------------------------------------------------------


def deficit_hint(dataset: Dataset, query: DonorQuery) -> DeficitHint | None:
    """Знаходить фільтр, який найбільше ріже вибірку.

    Перевірка чесна й проста: по черзі прибираємо кожен фільтр і дивимось,
    де приріст найбільший.
    """
    if not query.has_metric_filters:
        return None

    current = _count(dataset, query)
    candidates: list[DeficitHint] = []

    checks = (
        ("dr_min", f"DR від {_int(query.dr_min)}", query.dr_min),
        ("dr_max", f"DR до {_int(query.dr_max)}", query.dr_max),
        ("traffic_min", f"трафік від {_int(query.traffic_min)}", query.traffic_min),
        ("traffic_max", f"трафік до {_int(query.traffic_max)}", query.traffic_max),
    )

    for field_name, label, value in checks:
        if value is None:
            continue
        without = _count(dataset, query.replace(**{field_name: None}))
        if without > current:
            candidates.append(
                DeficitHint(filter_label=label, current_count=current, without_filter_count=without)
            )

    if not candidates:
        return None
    return max(candidates, key=lambda hint: hint.gain)


# ---------------------------------------------------------------------------
# Усе разом
# ---------------------------------------------------------------------------


def build_recommendations(dataset: Dataset, query: DonorQuery) -> Recommendations:
    """Збирає всі підказки для одного результату.

    Якщо база недоступна — підказок немає, і це нормально.
    """
    if not dataset.available or dataset.is_empty:
        return Recommendations()

    # Та сама нормалізація, що й у run_query: щоб рекомендації рахувалися
    # по тому самому запиту, що й основний результат.
    query = normalize_query(dataset, query)

    return Recommendations(
        same_language=same_language_suggestions(dataset, query),
        same_region=same_region_suggestions(dataset, query),
        relaxed=relaxed_suggestions(dataset, query),
        reserve=reserve_group(dataset, query) if query.kind is QueryKind.COUNTRY else None,
        deficit=deficit_hint(dataset, query),
    )


def _int(value: float | None) -> str:
    """Число без хвоста «.0»: 30.0 → «30»."""
    if value is None:
        return "-"
    return str(int(value)) if float(value).is_integer() else str(round(value, 1))


def summary_line(result_count: int, recommendations: Recommendations) -> str:
    """Коротка підказка для менеджера — як сформулювати пропозицію клієнту."""
    extra = recommendations.extra_total
    if extra == 0:
        return ""
    return (
        f"Можна запропонувати {result_count} максимально релевантних донорів "
        f"і ще до {extra} із суміжних гео."
    )


__all__ = [
    "DeficitHint",
    "Recommendations",
    "ReserveGroup",
    "Suggestion",
    "build_recommendations",
    "deficit_hint",
    "relaxed_suggestions",
    "reserve_group",
    "same_language_suggestions",
    "same_region_suggestions",
    "summary_line",
]
