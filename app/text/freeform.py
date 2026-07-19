"""Розбір запиту, написаного вільним текстом.

    «Скільки у нас донорів по Британії в Меджику з трафіком від 1?»
      → база Меджик, країна Британія, трафік від 1

Працює БЕЗ нейромережі (LLM_PROVIDER=none): країни й мови впізнає словник,
числа — прості правила. Це швидко, безкоштовно й не потребує інтернету.

Місце під LLM залишено: функція parse_free_text повертає той самий
DonorQuery, який згодом зможе будувати й нейромережа. Решті бота байдуже,
хто саме розібрав запит.

Числа шукаються за ключовими словами поруч:

    «трафік від 100»   → traffic_min = 100
    «DR від 20 до 40»  → dr_min = 20, dr_max = 40

Якщо в запиті просто «від 10» без слова «трафік» чи «DR» — фільтр НЕ
ставиться. Краще перепитати, ніж мовчки застосувати не той фільтр.

═══════════════════════════════════════════════════════════════════════════
ФРАЗИ СКАСУВАННЯ
═══════════════════════════════════════════════════════════════════════════

Користувач має вміти не лише ЗАДАТИ фільтр, а й ЗНЯТИ його словами:

    «DR не важливий», «трафік будь-який»   → метрика без обмежень
    «всі мови», «будь-яка мова»            → мова не важлива
    «будь-яка країна», «всі країни»        → країна не важлива

Це не косметика. Без такої фрази запит «будь-яка країна, DR від 50» бот
раніше взагалі не розумів — і тоді мовчки лишався активним ПОПЕРЕДНІЙ
запит із чужими фільтрами.

ЧОМУ ЧИСЛО СИЛЬНІШЕ ЗА СЛОВО. Спершу шукається число, і лише якщо його
немає — перевіряються слова скасування. Інакше в запиті

    «будь-яка країна др від 50»

слово «будь-яка» (яке стосується КРАЇНИ) вимкнуло б фільтр DR — саме такий
баг тут і був. Явно вказане число завжди перемагає розпливчасте «будь-який».
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.analytics.query import Dimension, DonorQuery
from app.dictionary.normalize import normalize_text
from app.dictionary.resolver import scan_entities

# Як користувач може назвати кожну базу.
_SECTION_WORDS: dict[str, tuple[str, ...]] = {
    "magic": ("меджик", "magic", "мэджик", "меджику", "меджика"),
    "mordy": ("морди", "морд", "мордах", "мордами", "mordy"),
    "submits": ("сабміт", "сабмит", "submits", "сабмітах"),
}

# Слова, якими знімають обмеження з метрики: «DR не важливий».
_CANCEL_MARKER = (
    r"(?:не\s+важлив\w*|не\s+важн\w*|неважлив\w*|без\s+обмеж\w*|будь[-\s]?як\w*"
    r"|байдуж\w*|не\s+має\s+значен\w*|не\s+потріб\w*|any)"
)

# Число: підтримує «1 200», «1,200», «1.5k».
_NUMBER = r"(\d[\d\s.,]*\s*[kкmм]?)"

# Розділювачі частин запиту: кома, крапка з комою, сполучник «і»/«та».
#
# Кома між цифрами НЕ розділяє: у «1,200» це роздільник тисяч, а не межа
# фрази. Саме тому в регулярці стоїть ,(?!\d) — «кома, за якою не цифра».
_CLAUSE_SPLIT = re.compile(r"[;]|,(?!\d)|\s+і\s+|\s+та\s+|\s+and\s+")


def _phrases(*items: str) -> tuple[str, ...]:
    """Готує фрази до порівняння — так само, як нормалізується запит."""
    return tuple(normalize_text(item) for item in items)


# Фрази «мова не важлива». Порядок не має значення: шукається найдовша.
_ANY_LANGUAGE_PHRASES = _phrases(
    "всі мови", "усі мови", "всіма мовами", "усіма мовами", "всі мовами",
    "будь-яка мова", "будь-яку мову", "будь-якою мовою", "будь яка мова",
    "мова не важлива", "мова не важливa", "мова неважлива", "мова не важлить",
    "мова будь-яка", "мова будь яка", "мова не має значення",
    "без урахування мови", "без огляду на мову", "без мови",
    "не враховувати мову", "незалежно від мови",
    "any language", "all languages",
)  # fmt: skip

# Фрази «країна не важлива».
_ANY_COUNTRY_PHRASES = _phrases(
    "будь-яка країна", "будь-яку країну", "будь-якій країні", "будь-якою країною",
    "будь яка країна", "будь-яке гео", "будь-який гео",
    "всі країни", "усі країни", "всіх країн", "усіх країн", "по всіх країнах",
    "країна не важлива", "країна неважлива", "країна не має значення",
    "гео не важливе", "гео не важливо", "гео не має значення",
    "без урахування країни", "без прив'язки до країни", "без прив'язки до гео",
    "без країни", "без гео", "не враховувати країну", "незалежно від країни",
    "any country", "all countries",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """Результат розбору вільного тексту."""

    query: DonorQuery
    understood: bool
    """False — не впізнали нічого. Тоді бот просить уточнити."""

    section_named: bool
    """Чи користувач сам назвав базу (чи взяли за замовчуванням)."""

    mentioned: frozenset[str] = frozenset()
    """Виміри, про які в цьому повідомленні сказали ЯВНО — задали або зняли.

    Потрібно, щоб бот міг відрізнити «щойно задане» від «успадкованого»
    і чесно позначити друге в резюме.
    """

    cancelled: frozenset[str] = frozenset()
    """Виміри, які в цьому повідомленні явно ЗНЯЛИ («всі мови»)."""

    @property
    def needs_clarification(self) -> bool:
        return not self.understood


@dataclass(frozen=True, slots=True)
class _RangeResult:
    """Що вдалося дізнатися про одну метрику з тексту."""

    minimum: float | None = None
    maximum: float | None = None
    mentioned: bool = False
    cancelled: bool = False


def _to_number(raw: str) -> float | None:
    """Перетворює знайдений шматок тексту на число."""
    from app.data.parsing import parse_number

    return parse_number(raw)


def detect_section(text: str, default: str = "magic") -> tuple[str, bool]:
    """Визначає, про яку базу питають. Повертає (ключ, чи названо явно)."""
    normalized = normalize_text(text)
    for key, words in _SECTION_WORDS.items():
        if any(word in normalized for word in words):
            return key, True
    return default, False


def _clauses(text: str) -> list[str]:
    """Ріже запит на частини по комах і сполучниках.

    Це принципово для правильного розбору. У запиті

        «трафіком від 1, DR не важливий»

    слова «не важливий» стосуються ЛИШЕ DR. Якби ми шукали їх по всьому
    тексту, вони помилково вимкнули б і фільтр трафіку — бот повернув би
    зовсім інше число, нічого не повідомивши.
    """
    return [part.strip() for part in _CLAUSE_SPLIT.split(text) if part.strip()]


def _is_cancelled(clause: str, keyword: str) -> bool:
    """Чи сказано, що ця метрика не важлива.

    Слово-заперечення має стояти ПОРУЧ із назвою метрики:

        «dr не важливий»        → так (одразу після)
        «будь-який dr»          → так (одразу перед)
        «будь-яка країна др»    → ні  (між ними ціле слово «країна»)

    Остання перевірка й рятує від бага, коли «будь-яка» від країни
    вимикало фільтр DR.
    """
    escaped = re.escape(keyword)
    after = rf"{escaped}\W*(?:\w+\s+){{0,2}}{_CANCEL_MARKER}"
    before = rf"{_CANCEL_MARKER}\s+{escaped}"
    return bool(re.search(after, clause) or re.search(before, clause))


def _extract_range(text: str, keywords: tuple[str, ...]) -> _RangeResult:
    """Дістає діапазон «від … до …» для однієї метрики.

    Порядок перевірок принциповий: СПЕРШУ число, і лише потім слова
    скасування. Пояснення — у докстрінгу модуля.
    """
    for clause in _clauses(text):
        keyword = next((k for k in keywords if k in clause), None)
        if keyword is None:
            continue

        # Числа беремо з тексту після ключового слова.
        window = clause[clause.find(keyword) :]

        both = re.search(rf"від\s*{_NUMBER}\s*до\s*{_NUMBER}", window)
        if both:
            return _RangeResult(
                _to_number(both.group(1)), _to_number(both.group(2)), mentioned=True
            )

        only_min = re.search(rf"(?:від|більше|более|more than|from|>=?)\s*{_NUMBER}", window)
        only_max = re.search(rf"(?:до|максимум|не більше|менше|<=?)\s*{_NUMBER}", window)

        minimum = _to_number(only_min.group(1)) if only_min else None
        maximum = _to_number(only_max.group(1)) if only_max else None
        if minimum is not None or maximum is not None:
            return _RangeResult(minimum, maximum, mentioned=True)

        # Ключове слово є, а «від»/«до» немає — можливо, просто «трафік 100».
        bare = re.search(rf"{re.escape(keyword)}\D{{0,3}}{_NUMBER}", window)
        if bare:
            return _RangeResult(_to_number(bare.group(1)), None, mentioned=True)

        # Числа немає — тоді дивимось, чи метрику навмисно не вимкнули.
        if _is_cancelled(clause, keyword):
            return _RangeResult(mentioned=True, cancelled=True)

    return _RangeResult()


def _find_cancel_phrases(normalized: str) -> tuple[frozenset[str], str]:
    """Шукає фрази на кшталт «всі мови» та «будь-яка країна».

    Повертає (які виміри знято, текст із затертими фразами).

    Затирання важливе: інакше словник міг би спробувати впізнати країну
    чи мову всередині самої фрази скасування.
    """
    cancelled: set[str] = set()
    text = normalized

    for dimension, phrases in (
        (Dimension.LANGUAGE, _ANY_LANGUAGE_PHRASES),
        (Dimension.COUNTRY, _ANY_COUNTRY_PHRASES),
    ):
        # Довші фрази перевіряємо першими: «будь-яка країна» важливіше
        # за «без країни», якщо раптом збіглися.
        for phrase in sorted(phrases, key=len, reverse=True):
            position = text.find(phrase)
            if position == -1:
                continue
            cancelled.add(dimension)
            text = text[:position] + " " * len(phrase) + text[position + len(phrase) :]
            break

    return frozenset(cancelled), text


def parse_free_text(text: str, *, default_section: str = "magic") -> ParsedQuery:
    """Перетворює вільний текст на запит до бази."""
    normalized = normalize_text(text)
    section, section_named = detect_section(text, default_section)

    # Крок 1: фрази скасування — і одразу прибираємо їх із тексту.
    cancelled, remaining = _find_cancel_phrases(normalized)

    # Крок 2: країна й мова — шукаються вже в очищеному тексті.
    entities = scan_entities(remaining)

    # Крок 3: метрики.
    traffic = _extract_range(remaining, ("трафік", "трафик", "traffic", "відвідув"))
    dr = _extract_range(remaining, ("dr", "др", "рейтинг домену"))

    country = None if Dimension.COUNTRY in cancelled else entities.country
    language = None if Dimension.LANGUAGE in cancelled else entities.language

    query = DonorQuery(
        section_key=section,
        country=country,
        language=language,
        # Явні глобальні зони («.com») теж є фільтром — але лише коли країни
        # не назвали, інакше вони суперечили б одна одній.
        zones=() if (country or Dimension.COUNTRY in cancelled) else entities.global_zones,
        dr_min=dr.minimum,
        dr_max=dr.maximum,
        traffic_min=traffic.minimum,
        traffic_max=traffic.maximum,
    )

    # Про що саме сказали в цьому повідомленні — задали або зняли.
    mentioned = set(cancelled)
    if entities.country or entities.global_zones:
        mentioned.add(Dimension.COUNTRY)
    if entities.language:
        mentioned.add(Dimension.LANGUAGE)
    if traffic.mentioned:
        mentioned.add(Dimension.TRAFFIC)
    if dr.mentioned:
        mentioned.add(Dimension.DR)
    if traffic.cancelled:
        cancelled = cancelled | {Dimension.TRAFFIC}
    if dr.cancelled:
        cancelled = cancelled | {Dimension.DR}

    # Скасування — теж зрозумілий намір, а не «нічого не зрозуміло».
    understood = bool(mentioned or section_named)

    return ParsedQuery(
        query=query,
        understood=understood,
        section_named=section_named,
        mentioned=frozenset(mentioned),
        cancelled=frozenset(cancelled),
    )


CLARIFICATION_TEXT = (
    "🤔 Не вдалося точно розпізнати запит.\n\n"
    "Спробуйте вказати базу, гео та потрібні метрики. Наприклад:\n"
    "• <code>Меджик, Британія, трафік від 1, DR не важливий</code>\n"
    "• <code>скільки німецькомовних донорів з DR від 20</code>\n"
    "• <code>Морди, .de, трафік від 100</code>\n\n"
    "Зняти зайвий фільтр теж можна словами:\n"
    "• <code>всі мови</code> · <code>будь-яка країна</code> · <code>DR не важливий</code>\n\n"
    "⚠️ Попередній запит поки лишається активним — подивитися його можна "
    "командою /filters, скинути повністю — /reset."
)
