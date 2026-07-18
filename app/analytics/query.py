"""Опис запиту: що саме користувач хоче порахувати.

Запит збирається однаково, звідки б він не прийшов — з кнопок меню, з
майстер-запиту чи з вільного тексту. Далі його виконує engine.py.

П'ять видів запиту. Різниця між ними не технічна, а змістова: вони
відповідають на різні питання й тому по-різному показуються.

    КРАЇНА    «Німеччина», «.de»   ядро = доменна зона .de
                                   + окремим рядком: скільки німецькомовних
                                     донорів є ПОЗА зоною .de
    МОВА      «німецькою»          ядро = колонка мови
    ЗОНА      «.com»               ядро = конкретна зона (нікому не належить)
    РАЗОМ     країна + мова        ядро = перетин обох умов
    МЕТРИКИ   тільки DR/трафік     ядро = вся база з фільтрами
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.dictionary.countries import Country
from app.dictionary.languages import Language


class QueryKind(StrEnum):
    """Вид запиту. Визначає, як показувати результат."""

    COUNTRY = "country"
    LANGUAGE = "language"
    ZONE = "zone"
    COMBINED = "combined"
    METRICS = "metrics"


@dataclass(frozen=True, slots=True)
class DonorQuery:
    """Один запит до бази."""

    section_key: str
    """Яку базу питаємо: "magic", "mordy"."""

    country: Country | None = None
    language: Language | None = None
    zones: tuple[str, ...] = ()
    """Явно вказані зони — коли користувач написав «.com», а не назву країни."""

    dr_min: float | None = None
    dr_max: float | None = None
    traffic_min: float | None = None
    traffic_max: float | None = None

    # -- вид запиту ----------------------------------------------------------

    @property
    def kind(self) -> QueryKind:
        """Визначає вид запиту за тим, що в ньому заповнено."""
        if self.country and self.language:
            return QueryKind.COMBINED
        if self.country:
            return QueryKind.COUNTRY
        if self.language:
            return QueryKind.LANGUAGE
        if self.zones:
            return QueryKind.ZONE
        return QueryKind.METRICS

    @property
    def has_metric_filters(self) -> bool:
        return any(
            value is not None
            for value in (self.dr_min, self.dr_max, self.traffic_min, self.traffic_max)
        )

    @property
    def is_empty(self) -> bool:
        """Запит без жодного фільтра — просто «скільки всього донорів»."""
        return self.kind is QueryKind.METRICS and not self.has_metric_filters

    # -- що саме фільтруємо --------------------------------------------------

    @property
    def core_zones(self) -> frozenset[str]:
        """Зони, за якими відбираємо донорів у ядро.

        Для запиту про країну це її справжні ccTLD. Для запиту про мову —
        порожньо: там зона не важлива.
        """
        if self.country:
            return frozenset(self.country.zones)
        return frozenset(self.zones)

    @property
    def core_languages(self) -> frozenset[str]:
        """Значення колонки «Мова», за якими відбираємо донорів."""
        return frozenset(self.language.data_keys) if self.language else frozenset()

    # -- зміна запиту (кнопки «додати фільтр», «пониження метрик») -----------

    def replace(self, **changes) -> DonorQuery:
        """Копія запиту зі зміненими полями.

        Запит незмінний (frozen), тому «змінити фільтр» = зробити нову копію.
        Так неможливо випадково зіпсувати запит, який уже виконується.
        """
        from dataclasses import replace as _replace

        return _replace(self, **changes)

    # -- опис для людини -----------------------------------------------------

    def describe(self) -> str:
        """Рядок «Запит:» у картці результату."""
        parts: list[str] = []

        if self.country:
            parts.append(f"{self.country.name_uk} ({self.country.zones_label})")
        if self.language:
            parts.append(f"мова {self.language.name_uk}")
        if not self.country and self.zones:
            parts.append(f"зона {', '.join(self.zones)}")

        parts.append(_describe_range("трафік", self.traffic_min, self.traffic_max))
        parts.append(_describe_range("DR", self.dr_min, self.dr_max))

        return ", ".join(part for part in parts if part) or "без фільтрів"


def _describe_range(label: str, minimum: float | None, maximum: float | None) -> str:
    """Описує діапазон людською мовою: «трафік від 10 до 100»."""
    if minimum is None and maximum is None:
        return f"{label} без обмеження"
    if maximum is None:
        return f"{label} від {_number(minimum)}"
    if minimum is None:
        return f"{label} до {_number(maximum)}"
    return f"{label} від {_number(minimum)} до {_number(maximum)}"


def _number(value: float | None) -> str:
    """Прибирає непотрібний хвіст «.0»: 10.0 → «10»."""
    if value is None:
        return "-"
    return str(int(value)) if float(value).is_integer() else str(value)
