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

from app.analytics.engine import passes_core, passes_metrics
from app.analytics.query import DonorQuery, QueryKind
from app.data.models import Dataset
from app.dictionary.countries import countries_in_region, countries_with_language

# Наскільки пом'якшуємо вимоги в підказках.
DR_RELAXATION = 10
TRAFFIC_DIVIDER = 2

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
    """Скільки донорів проходить запит."""
    return sum(1 for donor in dataset.donors if passes_core(donor, query))


# ---------------------------------------------------------------------------
# 1-2. Суміжні країни
# ---------------------------------------------------------------------------


def same_language_suggestions(dataset: Dataset, query: DonorQuery) -> tuple[Suggestion, ...]:
    """Країни з тією самою основною мовою: Німеччина → Австрія, Швейцарія."""
    country = query.country
    if country is None:
        return ()

    suggestions: list[Suggestion] = []
    for neighbour in countries_with_language(country.primary_language, exclude=country.code):
        neighbour_query = query.replace(country=neighbour)
        count = _count(dataset, neighbour_query)
        if count:
            suggestions.append(
                Suggestion(
                    label=f"{neighbour.flag} {neighbour.name_uk} ({neighbour.main_zone})",
                    count=count,
                    query=neighbour_query,
                )
            )

    suggestions.sort(key=lambda s: s.count, reverse=True)
    return tuple(suggestions[:MAX_SUGGESTIONS])


def same_region_suggestions(dataset: Dataset, query: DonorQuery) -> tuple[Suggestion, ...]:
    """Суміжні гео того самого регіону (ТЗ, розділ 13.1)."""
    country = query.country
    if country is None:
        return ()

    # Країни зі спільною мовою показуються окремим блоком — тут їх не дублюємо.
    already = {c.code for c in countries_with_language(country.primary_language)}

    suggestions: list[Suggestion] = []
    for neighbour in countries_in_region(country.region, exclude=country.code):
        if neighbour.code in already:
            continue
        neighbour_query = query.replace(country=neighbour)
        count = _count(dataset, neighbour_query)
        if count:
            suggestions.append(
                Suggestion(
                    label=f"{neighbour.flag} {neighbour.name_uk} ({neighbour.main_zone})",
                    count=count,
                    query=neighbour_query,
                )
            )

    suggestions.sort(key=lambda s: s.count, reverse=True)
    return tuple(suggestions[:MAX_SUGGESTIONS])


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


def reserve_group(dataset: Dataset, query: DonorQuery) -> ReserveGroup | None:
    """Основна група і запасна — щоб було що запропонувати понад ядро.

    Запас = донори основною мовою країни, яких НЕМАЄ в ядрі, з пом'якшеними
    вимогами. Саме тому ядро й запас можна складати без подвійного рахунку.
    """
    country = query.country
    if country is None or country.language is None:
        return None

    # Запам'ятовуємо ядро за номерами рядків — так порівняння точне.
    core_indices = {
        index for index, donor in enumerate(dataset.donors) if passes_core(donor, query)
    }
    if not core_indices:
        return None

    softer = query.replace(
        dr_min=max(0.0, query.dr_min - DR_RELAXATION) if query.dr_min else None,
        traffic_min=query.traffic_min / TRAFFIC_DIVIDER if query.traffic_min else None,
    )
    language_keys = country.language.data_keys

    reserve_count = sum(
        1
        for index, donor in enumerate(dataset.donors)
        if index not in core_indices  # головне: не рахуємо тих, хто вже в ядрі
        and donor.language in language_keys
        and passes_metrics(donor, softer)
    )

    if reserve_count == 0:
        return None

    return ReserveGroup(
        core_count=len(core_indices),
        reserve_count=reserve_count,
        reserve_label=f"донори мовою {country.language.name_uk} з м'якшими вимогами",
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
