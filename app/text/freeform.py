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
    «DR не важливий»   → фільтра немає

Якщо в запиті просто «від 10» без слова «трафік» чи «DR» — фільтр НЕ
ставиться. Краще перепитати, ніж мовчки застосувати не той фільтр.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.analytics.query import DonorQuery
from app.dictionary.normalize import normalize_text
from app.dictionary.resolver import scan_entities

# Як користувач може назвати кожну базу.
_SECTION_WORDS: dict[str, tuple[str, ...]] = {
    "magic": ("меджик", "magic", "мэджик", "меджику", "меджика"),
    "mordy": ("морди", "морд", "мордах", "мордами", "mordy"),
    "submits": ("сабміт", "сабмит", "submits", "сабмітах"),
}

# Слова, які означають «цей фільтр не потрібен».
_NO_LIMIT = ("не важлив", "не важн", "неважлив", "без обмеж", "будь-як", "байдуж", "не має значен")

# Число: підтримує «1 200», «1,200», «1.5k».
_NUMBER = r"(\d[\d\s.,]*\s*[kкmм]?)"

# Розділювачі частин запиту: кома, крапка з комою, сполучник «і»/«та».
#
# Кома між цифрами НЕ розділяє: у «1,200» це роздільник тисяч, а не межа
# фрази. Саме тому в регулярці стоїть ,(?!\d) — «кома, за якою не цифра».
_CLAUSE_SPLIT = re.compile(r"[;]|,(?!\d)|\s+і\s+|\s+та\s+|\s+and\s+")


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """Результат розбору: сам запит і чи вдалося щось зрозуміти."""

    query: DonorQuery
    understood: bool
    """False — не впізнали нічого. Тоді бот просить уточнити."""

    section_named: bool
    """Чи користувач сам назвав базу (чи взяли за замовчуванням)."""

    @property
    def needs_clarification(self) -> bool:
        return not self.understood


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

    слова «не важливий» стосуються ЛИШЕ DR. Якби ми шукали їх у вікні
    навколо слова «трафік», вони помилково вимкнули б і фільтр трафіку —
    бот повернув би зовсім інше число, нічого не повідомивши.
    """
    return [part.strip() for part in _CLAUSE_SPLIT.split(text) if part.strip()]


def _extract_range(text: str, keywords: tuple[str, ...]) -> tuple[float | None, float | None]:
    """Дістає діапазон «від … до …» для вказаної метрики.

    Шукає ту частину запиту, де згадана метрика, і читає числа тільки з неї.
    """
    for clause in _clauses(text):
        keyword = next((k for k in keywords if k in clause), None)
        if keyword is None:
            continue

        # «DR не важливий» — фільтра не буде.
        if any(marker in clause for marker in _NO_LIMIT):
            return None, None

        # Числа беремо з тексту після ключового слова.
        window = clause[clause.find(keyword) :]

        both = re.search(rf"від\s*{_NUMBER}\s*до\s*{_NUMBER}", window)
        if both:
            return _to_number(both.group(1)), _to_number(both.group(2))

        only_min = re.search(rf"(?:від|більше|более|more than|from|>=?)\s*{_NUMBER}", window)
        only_max = re.search(rf"(?:до|максимум|не більше|менше|<=?)\s*{_NUMBER}", window)

        minimum = _to_number(only_min.group(1)) if only_min else None
        maximum = _to_number(only_max.group(1)) if only_max else None
        if minimum is not None or maximum is not None:
            return minimum, maximum

        # Ключове слово є, а «від»/«до» немає — можливо, просто «трафік 100».
        bare = re.search(rf"{re.escape(keyword)}\D{{0,3}}{_NUMBER}", window)
        if bare:
            return _to_number(bare.group(1)), None

    return None, None


def parse_free_text(text: str, *, default_section: str = "magic") -> ParsedQuery:
    """Перетворює вільний текст на запит до бази."""
    normalized = normalize_text(text)
    section, section_named = detect_section(text, default_section)
    entities = scan_entities(text)

    traffic_min, traffic_max = _extract_range(
        normalized, ("трафік", "трафик", "traffic", "відвідув")
    )
    dr_min, dr_max = _extract_range(normalized, ("dr", "др", "рейтинг домену"))

    query = DonorQuery(
        section_key=section,
        country=entities.country,
        language=entities.language,
        # Явні глобальні зони («.com») теж є фільтром — але лише коли країни
        # не назвали, інакше вони суперечили б одна одній.
        zones=entities.global_zones if not entities.country else (),
        dr_min=dr_min,
        dr_max=dr_max,
        traffic_min=traffic_min,
        traffic_max=traffic_max,
    )

    understood = bool(
        entities.country
        or entities.language
        or entities.global_zones
        or query.has_metric_filters
        or section_named
    )

    return ParsedQuery(query=query, understood=understood, section_named=section_named)


CLARIFICATION_TEXT = (
    "🤔 Не вдалося точно розпізнати запит.\n\n"
    "Спробуйте вказати базу, гео та потрібні метрики. Наприклад:\n"
    "• <code>Меджик, Британія, трафік від 1, DR не важливий</code>\n"
    "• <code>скільки німецькомовних донорів з DR від 20</code>\n"
    "• <code>Морди, .de, трафік від 100</code>\n\n"
    "Або скористайтеся кнопками — так швидше."
)
