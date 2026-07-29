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
from app.llm.provider import LLMError, LLMProvider
from app.logging_setup import get_logger

logger = get_logger(__name__)

# Скільки символів сирої відповіді моделі писати в лог при помилці розбору —
# щоб було видно, ЩО саме вона повернула (без ключа: це лише текст моделі).
RAW_LOG_LIMIT = 300

# Розділи, які ШІ може обрати (лише ті, що читають дані).
ALLOWED_SECTIONS = frozenset({"magic", "mordy"})

# Числові поля-фільтри, які приймаємо від ШІ. Усе поза цим списком — ігнорується.
# Стовпця «вихідні» (F) тут немає: якість фільтрується ЛИШЕ по заспамленості
# (spam_*, стовпець G). Якщо модель усе ж поверне outlinks_*, ми зведемо їх до
# spam_* нижче (це те саме — «вихідні» в запиті = заспамленість).
ALLOWED_METRIC_FIELDS = (
    "dr_min",
    "dr_max",
    "traffic_min",
    "traffic_max",
    "spam_min",
    "spam_max",
)

# Синоніми полів: «вихідні» → заспамленість. Приймаємо на випадок, якщо модель
# все ж використала стару назву — щоб не втратити фільтр і не порушити правило.
_METRIC_ALIASES = {
    "outlinks_min": "spam_min",
    "outlinks_max": "spam_max",
}

SYSTEM_PROMPT = (
    "Ти перетворюєш запит користувача про SEO-донорів на структурний фільтр у "
    "форматі JSON. Ти НЕ маєш доступу до даних, доменів чи списку донорів — "
    "тільки до переліку полів, країн і мов нижче.\n\n"
    "Поверни ЛИШЕ JSON (без пояснень і без markdown) з такими можливими "
    "полями, усі необов'язкові:\n"
    '  "section": "magic" або "mordy"\n'
    '  "countries": масив кодів країн (коли країн кілька)\n'
    '  "country": код однієї країни\n'
    '  "languages": масив кодів мов (коли мов кілька; OR-фільтр)\n'
    '  "language": код однієї мови (старий сумісний формат)\n'
    '  "dr_min","dr_max": DR (авторитетність), невід\'ємні числа\n'
    '  "traffic_min","traffic_max": трафік, невід\'ємні числа\n'
    '  "spam_min","spam_max": ЗАСПАМЛЕНІСТЬ (лише для mordy), невід\'ємні числа\n\n'
    "НАПРЯМОК ПОРОГІВ (не плутай):\n"
    "• DR і трафік — «більше = краще», тож «від N» → dr_min / traffic_min "
    "(за замовчуванням для них саме мінімум).\n"
    "• Заспамленість — «менше = краще», тож за замовчуванням «до N» → spam_max.\n\n"
    "ЗАСПАМЛЕНІСТЬ у базі mordy — це ЄДИНА метрика якості донора (одне поле). "
    "Окремого числового фільтра «вихідні лінки» НЕ існує — НЕ створюй його й НЕ "
    "повертай жодного поля про вихідні. Будь-яка згадка «заспамленість», «спам», "
    "«незаспамлені», АБО «вихідні / вихідних лінків / вихідних посилань» у "
    "значенні обмеження — це фільтр ЗАСПАМЛЕНОСТІ (spam_min/spam_max):\n"
    "• «заспамленість 20», «до 20», «до 20 вихідних», «до 20 вихідних лінків» → "
    '{"spam_max": 20}\n'
    '• «від 20», «більше 20», «понад 20» → {"spam_min": 20}\n'
    '• «заспамлені» (є спам) → {"spam_min": 1}\n'
    "• «НЕЗАСПАМЛЕНІ», «без спаму», «чисті» БЕЗ конкретного числа — НЕ став "
    "поріг: не додавай ні spam_min, ні spam_max узагалі. Виконай запит за рештою "
    "критеріїв (країна, DR, трафік); розподіл заспамленості бот покаже сам. "
    "Поріг заспамленості став ЛИШЕ коли користувач назвав конкретне число — для "
    "кожного «незаспамлений» означає різне, не вирішуй за користувача.\n\n"
    "Виправляй очевидні ОДРУКИ в назвах країн і мов і зводь їх до коду з каталогу "
    "(напр. «англьійською» → мова en, «нїмецькі» → країна de, «Мезжик» → "
    "section magic). Це НЕ «вигадування» — саме такого виправлення від тебе й "
    "чекають; порожній фільтр через одрук — гірше, ніж розумний здогад.\n"
    "Використовуй ЛИШЕ коди з каталогу. Якщо поле справді не згадане — не вигадуй "
    "його (але одрук у наявному слові — виправляй, не відкидай).\n"
    "ПИТАЛЬНІ ФОРМУЛЮВАННЯ — ЦЕ ТЕЖ ФІЛЬТР, а не питання, на яке ти відповідаєш "
    "даними. «Скільки донорів по X?», «Є донори у Y?», «покажи, скільки в Мордах "
    "по США» — витягай із них базу/країну/мову/пороги так само, як із коротких "
    "«Морди, США». Ти лише будуєш фільтр; рахує завжди сам бот.\n"
    "Приклади:\n"
    '• «Скільки донорів у базі Морди по США?» → {"section": "mordy", "country": "us"}\n'
    '• «Є французькі донори з DR від 30?» → {"country": "fr", "dr_min": 30}\n'
    "Порожній {} повертай ЛИШЕ коли фільтрувати справді нема за чим — жодної згадки "
    "бази, країни, мови чи числового порога.\n"
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


def _strip_code_fences(text: str) -> str:
    """Прибирає markdown-огорожі ``` і ```json, лишаючи сам вміст."""
    return re.sub(r"```[a-zA-Z0-9]*", "", text)


def _iter_balanced_objects(text: str):
    """Породжує кожен збалансований об'єкт {...} верхнього рівня по черзі.

    Свій сканер (а не жадібний regex) — щоб зайві дужки в поясненні навколо JSON
    (напр. «Ось {результат}: {"country":"de"}») не збивали розбір: кандидати
    перебираємо, доки якийсь не розпарситься. Лапки й екрановані символи
    враховуємо, аби дужка всередині рядка не рахувалась."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : i + 1]
                    break
        start = text.find("{", start + 1)


def _merge_payloads(objects: list[dict]) -> dict:
    """Зливає КІЛЬКА JSON-об'єктів від моделі в один фільтр без втрат.

    Навіщо. На запити типу «меджик і морди, британія і німеччина» модель інколи
    відповідає кількома об'єктами поспіль (по одному на базу/країну):
    `{"section":"magic","country":"gb"},{"section":"mordy","country":"de"}`.
    Раніше брався ЛИШЕ перший — і мовчки губились і бази, і країни (баг зачіпав
    навіть одно-базові multi-country запити).

    Правило злиття:
      * країни з усіх об'єктів (`country` і `countries`) збираються в спільний
        список `countries` (дедуп, порядок збережено) — жодна не втрачається;
      * решта полів — перше непорожнє значення виграє (стабільно, передбачувано).

    Секцію навмисно НЕ зливаємо в список: контракт лишається одно-значним,
    а рішення «одна база чи обидві» ухвалюється окремо — детекцією тексту в
    run_ai_query (Варіант C). Так межі безпеки й whitelist не змінюються.
    """
    merged: dict = {}
    countries: list[str] = []
    seen: set[str] = set()

    def add_country(code: object) -> None:
        if isinstance(code, str):
            key = code.strip().lower()
            if key and key not in seen:
                seen.add(key)
                countries.append(key)

    for obj in objects:
        add_country(obj.get("country"))
        raw_list = obj.get("countries")
        if isinstance(raw_list, list | tuple):
            for code in raw_list:
                add_country(code)
        for field, value in obj.items():
            if field in ("country", "countries"):
                continue
            merged.setdefault(field, value)

    if countries:
        merged["countries"] = countries
        merged.pop("country", None)
    return merged


def _parse_json(raw: str) -> dict | None:
    """Дістає JSON-об'єкт(и) з відповіді, навіть якщо модель обгорнула їх у текст
    чи markdown-блок (```json). Повертає None, якщо валідного об'єкта немає.

    Якщо об'єктів кілька (модель розбила запит по базах/країнах) — не губимо їх
    мовчки, як раніше, а зливаємо в один фільтр (_merge_payloads) і логуємо факт.
    """
    if not raw:
        return None
    text = _strip_code_fences(raw)
    objects: list[dict] = []
    for candidate in _iter_balanced_objects(text):
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue  # цей кандидат — не JSON, пробуємо наступний
        if isinstance(payload, dict):
            objects.append(payload)

    if not objects:
        return None
    if len(objects) == 1:
        return objects[0]

    logger.warning(
        "ШІ повернув %d JSON-об'єктів замість одного — зливаю в один фільтр без втрат",
        len(objects),
    )
    return _merge_payloads(objects)


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

    languages: list = []
    if isinstance(payload.get("languages"), list | tuple):
        seen_languages: set[str] = set()
        for code in payload["languages"]:
            language = language_by_code(code.strip().lower()) if isinstance(code, str) else None
            if language is not None and language.code not in seen_languages:
                seen_languages.add(language.code)
                languages.append(language)
    elif isinstance(payload.get("language"), str):
        legacy_language = language_by_code(payload["language"].strip().lower())
        if legacy_language is not None:
            languages.append(legacy_language)

    metrics: dict[str, float] = {}
    for field in ALLOWED_METRIC_FIELDS:
        number = _coerce_number(payload.get(field))
        if number is not None:
            metrics[field] = number
    # «вихідні» від моделі → заспамленість (той самий фільтр по стовпцю G). Явно
    # заданий spam_* не перезаписуємо.
    for alias, target in _METRIC_ALIASES.items():
        number = _coerce_number(payload.get(alias))
        if number is not None:
            metrics.setdefault(target, number)

    is_multi = len(countries) >= 2
    query = DonorQuery(
        section_key=section,
        countries=tuple(countries) if is_multi else (),
        country=None if is_multi else (countries[0] if countries else single_country),
        languages=tuple(languages),
        **metrics,
    )

    if not (query.country or query.countries or query.languages or query.has_metric_filters):
        return None
    return query


class LLMInterpreter:
    """Обгортка: текст → (виклик моделі) → перевірений DonorQuery або None."""

    def __init__(self, provider: LLMProvider, *, catalog: str | None = None) -> None:
        self._provider = provider
        self._system = f"{SYSTEM_PROMPT}\n{catalog or build_catalog()}"

    async def interpret(self, text: str) -> DonorQuery | None:
        """Текст → DonorQuery. Кидає LLMError на будь-якій невдачі виклику чи
        розбору (ловить її вже AIService); None — коли модель відповіла валідно,
        але жоден дозволений фільтр не впізнано (тихий фолбек на словник)."""
        raw = await self._provider.complete(self._system, text)
        payload = _parse_json(raw)
        if payload is None:
            # Текст є, але JSON у ньому не знайшли — показуємо в лог, ЩО повернула
            # модель (перші символи), щоб причина була видна одразу.
            logger.error(
                "ШІ повернув відповідь без валідного JSON (перші %d симв.): %r",
                RAW_LOG_LIMIT,
                raw[:RAW_LOG_LIMIT],
            )
            raise LLMError("не вдалося витягти JSON з відповіді ШІ", stage="unparsable")
        return interpret_json(payload)
