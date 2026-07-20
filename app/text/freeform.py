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
взагалі не розумів — і тоді мовчки лишався активним ПОПЕРЕДНІЙ запит із
чужими фільтрами.

Сам розбір вимірів живе в dimensions.py — там один спільний механізм для
всіх вимірів одразу. Цей модуль лише складає з його результату готовий
запит. Так правила («число сильніше за слово», «вимір читає лише свій
шматок тексту») неможливо випадково застосувати до одного виміру й забути
про інший.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analytics.query import Dimension, DonorQuery
from app.dictionary.normalize import normalize_text
from app.dictionary.resolver import scan_entities
from app.text.dimensions import active_dimensions, resolve_dimensions

# Як користувач може назвати кожну базу.
_SECTION_WORDS: dict[str, tuple[str, ...]] = {
    "magic": ("меджик", "magic", "мэджик", "меджику", "меджика"),
    "mordy": ("морди", "морд", "мордах", "мордами", "mordy"),
    "submits": ("сабміт", "сабмит", "submits", "сабмітах"),
}


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


def detect_section(text: str, default: str = "magic") -> tuple[str, bool]:
    """Визначає, про яку базу питають. Повертає (ключ, чи названо явно)."""
    normalized = normalize_text(text)
    for key, words in _SECTION_WORDS.items():
        if any(word in normalized for word in words):
            return key, True
    return default, False


def parse_free_text(text: str, *, default_section: str = "magic") -> ParsedQuery:
    """Перетворює вільний текст на запит до бази.

    Уся робота з вимірами — один виклик resolve_dimensions. Він розбирає
    країну, мову, трафік і DR однаковим кодом за однаковими правилами, тож
    забути якесь правило для одного з них неможливо.
    """
    normalized = normalize_text(text)
    section, section_named = detect_section(text, default_section)

    # Крок 1: спільний механізм. Повертає що знайдено по кожному виміру
    # і текст, з якого розібране вже прибрано.
    matches, remaining = resolve_dimensions(normalized)

    # Крок 2: країну й мову шукаємо в очищеному тексті — у ньому вже немає
    # ні «будь-яка країна», ні «всі мови», тому словник їх не сплутає.
    entities = scan_entities(remaining)

    def cancelled_dimension(dimension: str) -> bool:
        match = matches.get(dimension)
        return match is not None and match.cancelled

    def limits(dimension: str) -> tuple[float | None, float | None]:
        match = matches.get(dimension)
        return (None, None) if match is None else (match.minimum, match.maximum)

    country = None if cancelled_dimension(Dimension.COUNTRY) else entities.country
    language = None if cancelled_dimension(Dimension.LANGUAGE) else entities.language
    dr_min, dr_max = limits(Dimension.DR)
    traffic_min, traffic_max = limits(Dimension.TRAFFIC)

    query = DonorQuery(
        section_key=section,
        country=country,
        language=language,
        # Явні глобальні зони («.com») теж є фільтром — але лише коли країни
        # не назвали, інакше вони суперечили б одна одній.
        zones=() if (country or cancelled_dimension(Dimension.COUNTRY)) else entities.global_zones,
        dr_min=dr_min,
        dr_max=dr_max,
        traffic_min=traffic_min,
        traffic_max=traffic_max,
    )

    # Про що саме сказали в цьому повідомленні — задали або зняли.
    # Виміри без колонок у даних (вихідні лінки, заспамленість) сюди не
    # потрапляють: їхні фрази розпізнано й прибрано, але фільтра поки немає.
    active = active_dimensions()
    mentioned = {dimension for dimension in matches if dimension in active}
    cancelled = {
        dimension for dimension, match in matches.items() if match.cancelled and dimension in active
    }

    if entities.country or entities.global_zones:
        mentioned.add(Dimension.COUNTRY)
    if entities.language:
        mentioned.add(Dimension.LANGUAGE)

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
