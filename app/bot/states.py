"""Стани майстер-запиту і зберігання поточного запиту користувача.

«Стан» — це те, на якому кроці зараз користувач. Бот має пам'ятати, що
він щойно спитав країну, щоб правильно зрозуміти наступне повідомлення.

Поточний запит зберігається між кроками й живе, поки користувач не
натисне «Скинути» (вимога ТЗ, розділ 29).
"""

from __future__ import annotations

from typing import Any

from aiogram.fsm.state import State, StatesGroup

from app.analytics.query import DonorQuery
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


def query_to_state(query: DonorQuery) -> dict[str, Any]:
    """Розкладає запит на прості значення для збереження."""
    return {
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


def summary_lines(query: DonorQuery, section_title: str) -> str:
    """Резюме фільтрів перед запуском (ТЗ, розділ 30)."""
    country = query.country.name_uk if query.country else "не обрано"
    language = query.language.name_uk if query.language else "не обрано"

    def limit(minimum: float | None, maximum: float | None) -> str:
        if minimum is None and maximum is None:
            return "не важливо"
        if maximum is None:
            return f"від {_clean(minimum)}"
        if minimum is None:
            return f"до {_clean(maximum)}"
        return f"від {_clean(minimum)} до {_clean(maximum)}"

    return (
        "<b>Перевірте параметри запиту:</b>\n\n"
        f"🗂 <b>База:</b> {section_title}\n"
        f"🌍 <b>Країна:</b> {country}\n"
        f"📊 <b>Трафік:</b> {limit(query.traffic_min, query.traffic_max)}\n"
        f"📈 <b>DR:</b> {limit(query.dr_min, query.dr_max)}\n"
        f"🗣 <b>Мова:</b> {language}"
    )


def _clean(value: float | None) -> str:
    if value is None:
        return "—"
    return str(int(value)) if float(value).is_integer() else str(value)
