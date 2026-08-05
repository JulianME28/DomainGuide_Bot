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
    """Крок «КРАЇНА» — визначається за доменною зоною."""

    geo = State()
    """Крок «ГЕО (країна трафіку)» — фільтр по колонці GEO, окремо від країни."""

    traffic = State()
    dr = State()
    spam = State()
    """Крок «Заспамленість» (стовпець G) — лише «Морди». Стовпець F («вихідні»)
    окремим кроком не фільтрується: він службовий (відсів мертвих сайтів)."""

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
    ai_query = State()
    """Режим «Індивідуальний запит»: текст ЗАВЖДИ йде через ШІ, не словником."""

    ai_chat = State()
    """Живий діалог з ШІ-консультантом: кожне наступне текстове
    повідомлення йде прямо в розмовну смугу з контекстом попередніх реплік.
    """


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
        "country_codes": [country.code for country in query.countries],
        "excluded_country_codes": [country.code for country in query.excluded_countries],
        "language_codes": [language.code for language in query.languages],
        # Compatibility with state produced by older bot versions.
        "language_code": query.language.code if query.language else None,
        "geo_code": query.geo.code if query.geo else None,
        "zones": list(query.zones),
        "dr_min": query.dr_min,
        "dr_max": query.dr_max,
        "traffic_min": query.traffic_min,
        "traffic_max": query.traffic_max,
        "spam_min": query.spam_min,
        "spam_max": query.spam_max,
    }


def query_from_state(data: dict[str, Any], *, default_section: str = "magic") -> DonorQuery:
    """Збирає запит назад із збережених значень."""
    country_code = data.get("country_code")
    country_codes = data.get("country_codes")
    countries = tuple(
        country
        for code in (country_codes if isinstance(country_codes, list) else ())
        if isinstance(code, str)
        if (country := country_by_code(code)) is not None
    )
    language_code = data.get("language_code")
    language_codes = data.get("language_codes")
    languages = tuple(
        language
        for code in (language_codes if isinstance(language_codes, list) else ())
        if isinstance(code, str)
        if (language := language_by_code(code)) is not None
    )
    if not languages and language_code:
        legacy_language = language_by_code(language_code)
        languages = (legacy_language,) if legacy_language is not None else ()

    geo_code = data.get("geo_code")
    excluded_country_codes = data.get("excluded_country_codes")
    excluded_countries = tuple(
        country
        for code in (excluded_country_codes if isinstance(excluded_country_codes, list) else ())
        if isinstance(code, str)
        if (country := country_by_code(code)) is not None
    )

    return DonorQuery(
        section_key=data.get("section_key") or default_section,
        country=country_by_code(country_code) if country_code else None,
        countries=countries,
        excluded_countries=excluded_countries,
        languages=languages,
        geo=country_by_code(geo_code) if geo_code else None,
        zones=tuple(data.get("zones") or ()),
        dr_min=data.get("dr_min"),
        dr_max=data.get("dr_max"),
        traffic_min=data.get("traffic_min"),
        traffic_max=data.get("traffic_max"),
        spam_min=data.get("spam_min"),
        spam_max=data.get("spam_max"),
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
    *,
    tracks_spam: bool = False,
    tracks_geo: bool = False,
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
    language = (
        ", ".join(language.name_uk for language in query.languages)
        if query.languages
        else "не обрано"
    )

    geo_name = query.geo.name_uk if query.geo else "не важливо"

    lines = [
        "<b>Перевірте параметри запиту:</b>",
        "",
        f"🗂 <b>База:</b> {section_title}",
        f"🌍 <b>Країна:</b> {country}{mark(Dimension.COUNTRY)}",
    ]

    # ГЕО (країна трафіку) — окремий фільтр по колонці GEO, лише для баз із нею.
    if tracks_geo:
        lines.append(f"🌐 <b>Гео (країна трафіку):</b> {geo_name}{mark(Dimension.GEO)}")

    lines.append(
        f"📊 <b>Трафік:</b> {limit(query.traffic_min, query.traffic_max)}{mark(Dimension.TRAFFIC)}"
    )
    lines.append(f"📈 <b>DR:</b> {limit(query.dr_min, query.dr_max)}{mark(Dimension.DR)}")

    # Заспамленість (стовпець G) — лише для баз, що мають цю колонку. Стовпця F
    # («вихідні») тут немає: числом він не фільтрується. Напрямок — знаком «≤»/«≥»,
    # бо менше = краще (щоб «20» не сплутали з «від 20»).
    if tracks_spam:
        lines.append(
            f"🧪 <b>Заспамленість:</b> "
            f"{_spam_limit(query.spam_min, query.spam_max)}{mark(Dimension.SPAM)}"
        )

    lines.append(f"🗣 <b>Мова:</b> {language}{mark(Dimension.LANGUAGE)}")

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
    languages = query.languages
    main_language = country.language
    language_names = ", ".join(language.name_uk for language in languages)

    return (
        f"⚠️ <b>Увага: країна і мова разом сильно звужують вибірку.</b>\n"
        f"Основна мова країни {country.name_uk} — "
        f"{main_language.name_uk if main_language else 'інша'}, "
        f"а у фільтрі стоять мови: {language_names}. "
        f"Буде враховано лише донорів однією з мов: {language_names} "
        f"у зоні {country.zones_label}.\n"
        f"Якщо це не те, що потрібно, приберіть мову кнопкою нижче."
    )


def _clean(value: float | None) -> str:
    if value is None:
        return "—"
    return str(int(value)) if float(value).is_integer() else str(value)


def _spam_limit(minimum: float | None, maximum: float | None) -> str:
    """Заспамленість у резюме майстра знаком «≤»/«≥» (менше = краще)."""
    if minimum is None and maximum is None:
        return "не важливо"
    if maximum == 0 and minimum in (None, 0):
        return "0 (тільки чисті, без мертвих)"
    if minimum is None:
        return f"≤ {_clean(maximum)}"
    if maximum is None:
        return f"≥ {_clean(minimum)}"
    return f"{_clean(minimum)}–{_clean(maximum)}"
