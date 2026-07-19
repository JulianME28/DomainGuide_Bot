"""Стани майстер-запиту і зберігання поточного запиту користувача.

«Стан» — це те, на якому кроці зараз користувач. Бот має пам'ятати, що
він щойно спитав країну, щоб правильно зрозуміти наступне повідомлення.

Поточний запит зберігається між кроками й живе, поки користувач не
натисне «Скинути» (вимога ТЗ, розділ 29).
"""

from __future__ import annotations

from typing import Any

from aiogram.fsm.state import State, StatesGroup

from app.analytics.query import Dimension, DonorQuery
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code


class Wizard(StatesGroup):
    """Кроки майстер-запиту."""

    country = State()
    """Крок «КРАЇНА» — саме так, а не «гео»: у даних немає поля гео,
    і питати треба те, на що бот справді може відповісти."""

    traffic = State()
    dr = State()
    language = State()
    confirm = State()


class Ask(StatesGroup):
    """Стани, коли бот чекає, що користувач щось напише."""

    country = State()
    language = State()
    zone = State()
    traffic = State()
    dr = State()
    free_text = State()


# ---------------------------------------------------------------------------
# Запит ↔ пам'ять бота
#
# У пам'яті стану можна зберігати лише прості значення (числа й рядки), тому
# країна й мова кладуться туди кодами, а не об'єктами.
# ---------------------------------------------------------------------------


FRESH_KEY = "fresh_dimensions"
"""Ключ у пам'яті стану: які виміри задані САМЕ ЗАРАЗ.

Усе інше, що має значення, вважається успадкованим з попереднього запиту
і позначається в резюме. Зберігається списком, бо в пам'яті стану можна
тримати лише прості типи.
"""


def query_to_state(query: DonorQuery, fresh: frozenset[str] | None = None) -> dict[str, Any]:
    """Розкладає запит на прості значення для збереження.

    fresh — виміри, задані цим-таки повідомленням. Якщо не вказано,
    вважаємо свіжим усе, що заповнене (звичайний випадок нового запиту).
    """
    return {
        FRESH_KEY: sorted(query.filled_dimensions if fresh is None else fresh),
        "section_key": query.section_key,
        "country_code": query.country.code if query.country else None,
        "language_code": query.language.code if query.language else None,
        "zones": list(query.zones),
        "dr_min": query.dr_min,
        "dr_max": query.dr_max,
        "traffic_min": query.traffic_min,
        "traffic_max": query.traffic_max,
    }


def query_from_state(data: dict[str, Any], *, default_section: str = "magic") -> DonorQuery:
    """Збирає запит назад із збережених значень."""
    country_code = data.get("country_code")
    language_code = data.get("language_code")

    return DonorQuery(
        section_key=data.get("section_key") or default_section,
        country=country_by_code(country_code) if country_code else None,
        language=language_by_code(language_code) if language_code else None,
        zones=tuple(data.get("zones") or ()),
        dr_min=data.get("dr_min"),
        dr_max=data.get("dr_max"),
        traffic_min=data.get("traffic_min"),
        traffic_max=data.get("traffic_max"),
    )


INHERITED_MARK = "(з попереднього запиту)"


def fresh_from_state(data: dict[str, Any]) -> frozenset[str]:
    """Виміри, задані САМЕ ЗАРАЗ, а не успадковані з попереднього запиту."""
    return frozenset(data.get(FRESH_KEY) or ())


def inherited_dimensions(query: DonorQuery, fresh: frozenset[str]) -> frozenset[str]:
    """Виміри, які щось містять, але задані НЕ в поточному кроці."""
    return query.filled_dimensions - fresh


def summary_lines(
    query: DonorQuery,
    section_title: str,
    fresh: frozenset[str] = frozenset(),
) -> str:
    """Резюме фільтрів перед запуском (ТЗ, розділ 30).

    Кожен фільтр, який лишився з попереднього запиту, підписаний
    «(з попереднього запиту)». Без цього підпису успадкування невидиме:
    людина бачить фільтр, якого не задавала, і мовчки отримує не ті числа.
    """
    inherited = inherited_dimensions(query, fresh)

    def mark(dimension: str) -> str:
        return f" <i>{INHERITED_MARK}</i>" if dimension in inherited else ""

    def limit(minimum: float | None, maximum: float | None) -> str:
        if minimum is None and maximum is None:
            return "не важливо"
        if maximum is None:
            return f"від {_clean(minimum)}"
        if minimum is None:
            return f"до {_clean(maximum)}"
        return f"від {_clean(minimum)} до {_clean(maximum)}"

    country = query.country.name_uk if query.country else "не обрано"
    language = query.language.name_uk if query.language else "не обрано"

    lines = [
        "<b>Перевірте параметри запиту:</b>",
        "",
        f"🗂 <b>База:</b> {section_title}",
        f"🌍 <b>Країна:</b> {country}{mark(Dimension.COUNTRY)}",
        f"📊 <b>Трафік:</b> {limit(query.traffic_min, query.traffic_max)}{mark(Dimension.TRAFFIC)}",
        f"📈 <b>DR:</b> {limit(query.dr_min, query.dr_max)}{mark(Dimension.DR)}",
        f"🗣 <b>Мова:</b> {language}{mark(Dimension.LANGUAGE)}",
    ]

    if inherited:
        lines.append("")
        lines.append(
            "<i>Підписані фільтри лишилися з попереднього запиту. "
            "Прибрати кожен окремо можна кнопками нижче.</i>"
        )

    conflict = conflict_warning(query)
    if conflict:
        lines.append("")
        lines.append(conflict)

    return "\n".join(lines)


def conflict_warning(query: DonorQuery) -> str:
    """Попередження, коли країна й мова разом сильно звужують вибірку.

    Приклад: країна Німеччина + мова англійська. Запит коректний, але
    рахує лише англомовні сайти в зоні .de — а людина, побачивши
    «Німеччина», зазвичай чекає геть іншого числа.
    """
    if not query.has_language_conflict:
        return ""

    country = query.country
    language = query.language
    main_language = country.language

    return (
        f"⚠️ <b>Увага: країна і мова разом сильно звужують вибірку.</b>\n"
        f"Основна мова країни {country.name_uk} — "
        f"{main_language.name_uk if main_language else 'інша'}, "
        f"а у фільтрі стоїть {language.name_uk}. "
        f"Буде враховано лише донорів мовою {language.name_uk} "
        f"у зоні {country.zones_label}.\n"
        f"Якщо це не те, що потрібно, приберіть мову кнопкою нижче."
    )


def _clean(value: float | None) -> str:
    if value is None:
        return "—"
    return str(int(value)) if float(value).is_integer() else str(value)
