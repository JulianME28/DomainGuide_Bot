"""Перетворення розмитого запиту на СТРУКТУРНИЙ фільтр через ШІ.

Що бачить ШІ: лише текст користувача + каталог доступних фільтрів, країн і мов
(коди й назви). Даних, доменів, донорів — НІКОЛИ.

Що повертає: JSON. Backend (`interpret_json`) перевіряє КОЖНЕ поле по whitelist:
невідомі поля ігноруються, невідомі коди країн/мов відкидаються, числа мають
бути невід'ємні. Тому ШІ фізично не може попросити те, чого бот не дозволяє.
"""

from __future__ import annotations

import json
import re

from app.analytics.query import DonorQuery
from app.dictionary.countries import COUNTRIES, country_by_code
from app.dictionary.languages import LANGUAGES, language_by_code
from app.llm.provider import LLMProvider

# Розділи, які ШІ може обрати (лише ті, що читають дані).
ALLOWED_SECTIONS = frozenset({"magic", "mordy"})

# Числові поля-фільтри, які приймаємо від ШІ. Усе поза цим списком — ігнорується.
ALLOWED_METRIC_FIELDS = (
    "dr_min",
    "dr_max",
    "traffic_min",
    "traffic_max",
    "outlinks_min",
    "outlinks_max",
    "spam_min",
    "spam_max",
)

SYSTEM_PROMPT = (
    "Ти перетворюєш запит користувача про SEO-донорів на структурний фільтр у "
    "форматі JSON. Ти НЕ маєш доступу до даних, доменів чи списку донорів — "
    "тільки до переліку полів, країн і мов нижче.\n\n"
    "Поверни ЛИШЕ JSON (без пояснень і без markdown) з такими можливими "
    "полями, усі необов'язкові:\n"
    '  "section": "magic" або "mordy"\n'
    '  "countries": масив кодів країн (коли країн кілька)\n'
    '  "country": код однієї країни\n'
    '  "language": код мови\n'
    '  "dr_min","dr_max","traffic_min","traffic_max": невід\'ємні числа\n'
    '  "outlinks_min","outlinks_max","spam_min","spam_max": числа (лише для mordy)\n\n'
    "Використовуй ЛИШЕ коди з каталогу. Якщо чогось не зрозумів — не вигадуй, "
    "просто не додавай це поле. Якщо запит зовсім незрозумілий — поверни {}.\n"
)


def build_catalog() -> str:
    """Каталог кодів для системного промпту: країни й мови (код=назва)."""
    countries = ", ".join(f"{c.code}={c.name_uk}" for c in COUNTRIES.values())
    languages = ", ".join(f"{lang.code}={lang.name_uk}" for lang in LANGUAGES.values())
    return f"Країни (код=назва): {countries}\n\nМови (код=назва): {languages}"


def _coerce_number(value: object) -> float | None:
    """Число ≥0 або None. Булеві та від'ємні — відкидаємо."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if number >= 0 else None
    return None


def _parse_json(raw: str) -> dict | None:
    """Дістає JSON-об'єкт із відповіді (навіть якщо навколо є зайвий текст)."""
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def interpret_json(payload: dict) -> DonorQuery | None:
    """Перетворює JSON від ШІ на DonorQuery, перевіряючи кожне поле по whitelist.

    Повертає None, якщо не лишилося жодного дозволеного фільтра — тоді бот
    поводиться так, ніби запит не зрозумів (тихий фолбек)."""
    raw_section = payload.get("section")
    section = raw_section if raw_section in ALLOWED_SECTIONS else "magic"

    # Країни: лишаємо тільки відомі коди, дедуп зі збереженням порядку.
    countries: list = []
    seen: set[str] = set()
    for code in payload.get("countries") or ():
        country = country_by_code(str(code).strip().lower()) if isinstance(code, str) else None
        if country is not None and country.code not in seen:
            seen.add(country.code)
            countries.append(country)

    single_country = None
    if isinstance(payload.get("country"), str):
        single_country = country_by_code(payload["country"].strip().lower())

    language = None
    if isinstance(payload.get("language"), str):
        language = language_by_code(payload["language"].strip().lower())

    metrics: dict[str, float] = {}
    for field in ALLOWED_METRIC_FIELDS:
        number = _coerce_number(payload.get(field))
        if number is not None:
            metrics[field] = number

    is_multi = len(countries) >= 2
    query = DonorQuery(
        section_key=section,
        countries=tuple(countries) if is_multi else (),
        country=None if is_multi else (countries[0] if countries else single_country),
        language=language,
        **metrics,
    )

    if not (query.country or query.countries or query.language or query.has_metric_filters):
        return None
    return query


class LLMInterpreter:
    """Обгортка: текст → (виклик моделі) → перевірений DonorQuery або None."""

    def __init__(self, provider: LLMProvider, *, catalog: str | None = None) -> None:
        self._provider = provider
        self._system = f"{SYSTEM_PROMPT}\n{catalog or build_catalog()}"

    async def interpret(self, text: str) -> DonorQuery | None:
        """Кидає LLMError на проблемі з викликом — ловить її вже AIService."""
        raw = await self._provider.complete(self._system, text)
        payload = _parse_json(raw)
        if payload is None:
            return None
        return interpret_json(payload)
