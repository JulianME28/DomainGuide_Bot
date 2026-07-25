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

import re
from dataclasses import dataclass

from app.analytics.query import Dimension, DonorQuery
from app.dictionary.countries import country_by_zone
from app.dictionary.normalize import find_zone_mentions, normalize_text
from app.dictionary.resolver import find_all_countries, find_country_match, scan_entities
from app.text.dimensions import SPECS, active_dimensions, resolve_dimensions

# Фрази-ПРОХАННЯ: слова в тій самій частині речення — це підказка для
# рекомендацій, а НЕ фільтр. «якщо мало — англомовні альтернативи» просить
# показати схожі варіанти, а не звужувати вибірку до англомовних.
_REQUEST_CONDITIONAL = r"якщо\s+(?:мало|замало|недостатньо)"
_REQUEST_MARKER = re.compile(
    r"(?:"
    + _REQUEST_CONDITIONAL
    + r"|альтернатив\w*|варіант\w*|запропон\w*|підбер\w*\s+схож\w*"
    + r"|схож\w*\s+варіант\w*|що\s+ще\s+є)"
)
_REQUEST_CONDITIONAL_RE = re.compile(_REQUEST_CONDITIONAL)

# Модифікатори запиту ЛИШЕ по доменній зоні: після них іде зона («.co.uk») або
# назва країни («Британія» → усі її ccTLD).
# «зона X», «у зоні X», «в зоні X», «доменна зона X», «тільки зона X», «лише зона X».
_ZONE_MODIFIER = re.compile(r"\b(?:тільки\s+|лише\s+|доменн\w*\s+|домен\s+)?(?:[ув]\s+)?зон\w*")

# Прикметникові форми заспамленості (так/ні), а не числовий фільтр:
# «незаспамлені» → заспамленість 0, «заспамлені» → заспамленість > 0.
# Лукахед (?!іст|ост) відсікає ІМЕННИК у всіх відмінках («заспамленість»,
# «заспамленості», «заспамленістю») — то числовий вимір / фраза скасування,
# їх розбирає resolve_dimensions.
_SPAM_FLAG = re.compile(r"\b(не)?заспамлен(?!іст|ост)\w*")

# Модифікатори GEO-фільтра: після них іде НАЗВА КРАЇНИ походження трафіку.
# «гео Польща», «geo Poland», «за гео Німеччина», «трафік з Польщі».
_GEO_MODIFIER = re.compile(r"\b(?:за\s+гео|гео|geo|трафік\w*\s+з|трафік\w*\s+із)\b")

# Скасування GEO-фільтра: «гео не важливо», «без гео», «гео будь-яке».
_GEO_CANCEL = re.compile(
    r"\b(?:без\s+(?:урахування\s+)?гео"
    r"|гео\s+(?:не\s+важлив\w*|неважлив\w*|будь[-\s]?як\w*|не\s+має\s+значен\w*))\b"
)


# Перелік БАЗ в одному запиті: «(Меджик + Морди)», «Меджик і Морди»,
# «в обох базах», «по обох базах», «всього по базах». Такий запит просить
# зведення по двох базах — так само, як коли базу не назвали зовсім.
_BASE_TOKEN = r"(?:меджик\w*|magic|мэджик|морд\w*|mordy)"
_PLUS_BASES = re.compile(_BASE_TOKEN + r"\s*\+\s*" + _BASE_TOKEN)
_BOTH_BASES = re.compile(
    r"(?:"
    + _BASE_TOKEN
    + r"\s*(?:\+|і|та|and)\s*"
    + _BASE_TOKEN
    + r"|обох\s+баз\w*"
    + r"|по\s+базах"
    + r"|усіма\s+базами|всіма\s+базами)"
)

# Слова-ПІДСУМОК: просять показати сумарне число по базах.
# «скільки всього», «всього», «сумарно», «разом», «загалом».
_SUMMARY = re.compile(r"\b(?:скільки\s+всього|всього|сумарно|разом|загалом)\b")


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _clean_request(text: str) -> str:
    """Прибирає з фрази-прохання провідний зворот «якщо мало» й розділові знаки.

    «якщо мало — англомовні альтернативи» → «англомовні альтернативи»."""
    text = re.sub(r"^\s*" + _REQUEST_CONDITIONAL + r"\s*", " ", text)
    text = text.strip(" —–-,:;")
    return re.sub(r"\s+", " ", text).strip()


def _extract_request(text: str) -> tuple[str, str, bool]:
    """Відділяє фрази-прохання від запиту-фільтра.

    Повертає (текст без прохання, впізнана фраза-прохання, чи був маркер).
    Якщо маркера немає — текст не чіпаємо взагалі. Якщо є — ділимо на частини
    за «;», комою й переносами рядка: частину з умовним «якщо мало» ріжемо на
    самому «якщо» (до нього — фільтри, від нього — прохання), а частину з
    іменником-маркером («альтернативи», «варіанти») вважаємо проханням цілком.
    Так «Німеччина, варіанти» лишає країну, а «варіанти» йде в прохання.
    """
    if not _REQUEST_MARKER.search(text):
        return text, "", False

    kept: list[str] = []
    requests: list[str] = []
    for part in re.split(r"[;,\n]", text):
        if not _REQUEST_MARKER.search(part):
            kept.append(part)
            continue
        conditional = _REQUEST_CONDITIONAL_RE.search(part)
        if conditional is not None:
            kept.append(part[: conditional.start()])
            requests.append(part[conditional.start() :])
        else:
            requests.append(part)

    cleaned = " ".join(part for part in kept if part.strip())
    hint = _clean_request(" ".join(requests))
    return cleaned, hint, True


def _extract_geo(text: str) -> tuple[object, bool, str]:
    """Витягує GEO-фільтр із нормалізованого тексту.

    Повертає (країна GEO або None, чи скасовано, текст без GEO-фрази). Країну
    після модифікатора затираємо разом із модифікатором — щоб «Польща» в «гео
    Польща» не сприйнялася ще й як звичайний країновий запит.
    """
    cancelled = False
    for match in list(_GEO_CANCEL.finditer(text)):
        text = _mask(text, match.start(), match.end())
        cancelled = True

    geo_country = None
    modifier = _GEO_MODIFIER.search(text)
    if modifier is not None:
        raw_tail = text[modifier.end() :]
        stripped = raw_tail.lstrip()
        # find_country_match всередині нормалізує (стягує пробіли), тому щоб
        # позиції збіглися, шукаємо в уже обрізаному зліва «хвості».
        offset = modifier.end() + (len(raw_tail) - len(stripped))
        head = stripped.split(",", 1)[0]
        found = find_country_match(head, allow_short=False)
        if found is not None:
            geo_country = found[0]
            text = _mask(text, modifier.start(), offset + found[1].end)

    return geo_country, cancelled, text


def _zones_of(zone: str) -> tuple[str, ...]:
    """Усі ccTLD країни, якій належить зона. Для глобальної — вона сама.

    «.co.uk» → («.co.uk», «.uk»): користувач просить доменну зону Британії, а в
    неї їх дві, і рахувати треба обидві (інакше число не збіжиться із зоновою
    складовою країнової картки). «.com» нікому не належить — лишається сама."""
    owner = country_by_zone(zone)
    return tuple(owner.zones) if owner is not None else (zone,)


def _extract_spam_flag(text: str) -> tuple[float | None, float | None, str]:
    """Розбирає прикметник заспамленості. Повертає (spam_min, spam_max, текст).

    «незаспамлені» → заспамленість = 0 (spam_max=0). «заспамлені» → заспамленість
    > 0 (spam_min=1, бо це кількість заспамлених лінків). Знайдене затираємо,
    щоб слово не заважало розбору решти."""
    match = _SPAM_FLAG.search(text)
    if match is None:
        return None, None, text

    negated = match.group(1) is not None
    text = _mask(text, match.start(), match.end())
    if negated:
        return None, 0.0, text
    return 1.0, None, text


def _extract_zone(text: str) -> tuple[tuple[str, ...], str]:
    """Витягує запит «лише по зоні» з нормалізованого тексту.

    Повертає (зони, текст без цієї фрази). Знайдене затираємо, щоб «.co.uk»
    після «у зоні» не перетворилося ще й на країновий запит із водоспадом.
    Фрази скасування («зона не важлива») тут НЕ обробляються — це робить
    спільний механізм resolve_dimensions.
    """
    modifier = _ZONE_MODIFIER.search(text)
    if modifier is None:
        return (), text

    raw_tail = text[modifier.end() :]
    stripped = raw_tail.lstrip()
    offset = modifier.end() + (len(raw_tail) - len(stripped))
    head = stripped.split(",", 1)[0]

    # 1) Явна зона: «у зоні .co.uk». find_zone_mentions не нормалізує текст,
    # тому позиції збігаються з head.
    mentions = find_zone_mentions(head)
    if mentions:
        zone, _start, end = mentions[0]
        return _zones_of(zone), _mask(text, modifier.start(), offset + end)

    # 2) Назва країни: «зона Британія» → усі її ccTLD.
    found = find_country_match(head, allow_short=False)
    if found is not None and found[0].zones:
        return tuple(found[0].zones), _mask(text, modifier.start(), offset + found[1].end)

    return (), text


# Як користувач може назвати кожну базу.
_SECTION_WORDS: dict[str, tuple[str, ...]] = {
    "magic": ("меджик", "magic", "мэджик", "меджику", "меджика"),
    "mordy": ("морди", "морд", "мордах", "мордами", "mordy"),
    "submits": ("сабміт", "сабмит", "submits", "сабмітах"),
}

# Слова, які лишаються в тексті після розбору списку країн, але країнами НЕ є:
# структурні («по», «донори»), назви баз і назви вимірів. Усе інше, що схоже
# на назву, вважаємо нерозпізнаною країною.
_STRUCTURAL_STOPWORDS = frozenset(
    {
        "по", "на", "для", "про", "и", "і", "та", "або", "or", "and", "the", "of",
        "for", "list", "донори", "донор", "донора", "донорів", "донорам", "донорами",
        "покажи", "показати", "скільки", "дай", "знайди", "знайти", "хочу", "треба",
        "потрібно", "база", "базі", "бази", "усі", "усіх", "все", "всі", "всіх",
        "гео", "geo",  # лишок модифікатора GEO, якщо країну після нього не впізнали
    }
)  # fmt: skip
_ALL_SECTION_WORDS = frozenset(word for words in _SECTION_WORDS.values() for word in words)
_DIMENSION_STEMS = tuple(stem for spec in SPECS for stem in spec.stems)


def _unrecognized_names(leftover: str) -> tuple[str, ...]:
    """Слова із залишку тексту, які схожі на назву країни, але не впізнані.

    Залишок — це текст, де вже затерто знайдені країни й мови. Викидаємо з
    нього службові слова, назви баз і назви вимірів (їхні стеми), а решту
    словесних токенів (≥3 літери) вважаємо нерозпізнаними назвами."""
    tokens = re.findall(r"[a-zа-яёєіїґ]{3,}", leftover)
    unrecognized: list[str] = []
    for token in tokens:
        if token in _ALL_SECTION_WORDS or token in _STRUCTURAL_STOPWORDS:
            continue
        if any(token.startswith(stem) for stem in _DIMENSION_STEMS):
            continue
        if token not in unrecognized:
            unrecognized.append(token)
    return tuple(unrecognized)


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

    unrecognized: tuple[str, ...] = ()
    """Назви зі списку країн, які не вдалося впізнати (лише для запиту-списку)."""

    request_marker: bool = False
    """Чи був у тексті маркер-прохання («якщо мало», «альтернативи»…). Тоді
    показуємо повну картку з блоком суміжних країн, а не зведення по обох базах."""

    both_bases: bool = False
    """Чи попросили ЯВНО обидві бази («(Меджик + Морди)», «в обох базах»). Тоді
    показуємо зведення навіть якщо назву бази в тексті згадали."""

    want_total: bool = False
    """Чи просили ПІДСУМОК по базах («скільки всього», перелік через «+»). Саме
    зведення показує підсумок завжди; цей прапорець робить «зрозумілим» навіть
    голий запит на кшталт «скільки всього», де інших фільтрів немає."""

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

    # Перелік баз і слово-підсумок шукаємо на СВІЖОМУ тексті — до того, як
    # витягування вимірів затре частину слів. «(Меджик + Морди)» → обидві бази;
    # «скільки всього» / перелік через «+» → показати сумарне число.
    both_bases = _BOTH_BASES.search(normalized) is not None
    plus_bases = _PLUS_BASES.search(normalized) is not None
    want_total = plus_bases or _SUMMARY.search(normalized) is not None

    # Крок 0-: фрази-прохання («якщо мало — англомовні альтернативи»). Прибираємо
    # НАЙПЕРШИМ, щоб слова прохання не стали фільтрами (напр. «англомовні» — мовою).
    normalized, request_hint, request_marker = _extract_request(normalized)

    # Крок 0: GEO-фільтр («гео Польща»). Витягуємо ПЕРШИМ і затираємо, щоб
    # країна після «гео» не сприйнялася ще й як звичайний країновий запит.
    geo_country, geo_cancelled, normalized = _extract_geo(normalized)
    geo = None if geo_cancelled else geo_country

    # Крок 0b: запит ЛИШЕ по зоні («у зоні .co.uk»). Теж витягуємо до розбору
    # країни — інакше «.co.uk» пішов би країновим запитом із водоспадом.
    explicit_zones, normalized = _extract_zone(normalized)

    # Крок 0c: прикметник заспамленості («незаспамлені» → 0, «заспамлені» → >0).
    flag_spam_min, flag_spam_max, normalized = _extract_spam_flag(normalized)

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

    # Явна зона перемагає країну: користувач попросив саме зону, без водоспаду.
    zone_cancelled = cancelled_dimension(Dimension.ZONE)
    if zone_cancelled:
        explicit_zones = ()
    if explicit_zones:
        country = None
    dr_min, dr_max = limits(Dimension.DR)
    traffic_min, traffic_max = limits(Dimension.TRAFFIC)
    outlinks_min, outlinks_max = limits(Dimension.OUTLINKS)
    spam_min, spam_max = limits(Dimension.SPAM)
    # Прикметникова заспамленість діє, лише коли числового фільтра немає.
    if (
        spam_min is None
        and spam_max is None
        and (flag_spam_min is not None or flag_spam_max is not None)
    ):
        spam_min, spam_max = flag_spam_min, flag_spam_max

    active = active_dimensions()
    metric_mentions = {dimension for dimension in matches if dimension in active}
    if flag_spam_min is not None or flag_spam_max is not None:
        metric_mentions.add(Dimension.SPAM)
    cancelled = {
        dimension for dimension, match in matches.items() if match.cancelled and dimension in active
    }

    # СПИСОК країн (≥2 в одному запиті) — окремий шлях: розклад по країнах і
    # унікальний підсумок. Метричні фільтри застосовуються до всіх країн.
    # Якщо явно попросили зону — це не список країн, а запит по зоні.
    if not cancelled_dimension(Dimension.COUNTRY) and not explicit_zones:
        countries_all, leftover = find_all_countries(remaining)
        if len(countries_all) >= 2:
            unrecognized = _unrecognized_names(leftover)
            multi_query = DonorQuery(
                section_key=section,
                countries=tuple(countries_all),
                geo=geo,
                unrecognized=unrecognized,
                dr_min=dr_min,
                dr_max=dr_max,
                traffic_min=traffic_min,
                traffic_max=traffic_max,
                outlinks_min=outlinks_min,
                outlinks_max=outlinks_max,
                spam_min=spam_min,
                spam_max=spam_max,
            )
            multi_mentioned = {Dimension.COUNTRY} | metric_mentions
            if geo_country is not None or geo_cancelled:
                multi_mentioned.add(Dimension.GEO)
            return ParsedQuery(
                query=multi_query,
                understood=True,
                section_named=section_named,
                mentioned=frozenset(multi_mentioned),
                cancelled=frozenset(cancelled | ({Dimension.GEO} if geo_cancelled else set())),
                unrecognized=unrecognized,
                both_bases=both_bases,
                want_total=want_total,
            )

    query = DonorQuery(
        section_key=section,
        country=country,
        language=language,
        geo=geo,
        # Явно попрошена зона («у зоні .co.uk») — найсильніша. Інакше беремо
        # глобальні зони («.com»), але лише коли країни не назвали, бо разом
        # вони суперечили б одна одній.
        zones=explicit_zones
        or (
            ()
            if (country or cancelled_dimension(Dimension.COUNTRY) or zone_cancelled)
            else entities.global_zones
        ),
        request_hint=request_hint,
        dr_min=dr_min,
        dr_max=dr_max,
        traffic_min=traffic_min,
        traffic_max=traffic_max,
        outlinks_min=outlinks_min,
        outlinks_max=outlinks_max,
        spam_min=spam_min,
        spam_max=spam_max,
    )

    # Про що саме сказали в цьому повідомленні — задали або зняли. Метричні
    # згадки й скасування вже пораховані вище (metric_mentions, cancelled).
    # Виміри без колонок у даних (вихідні лінки, заспамленість) у mentioned не
    # потрапляють: їхні фрази розпізнано, але фільтра поки немає.
    mentioned = set(metric_mentions)
    if entities.country or entities.global_zones:
        mentioned.add(Dimension.COUNTRY)
    if entities.language:
        mentioned.add(Dimension.LANGUAGE)
    if geo_country is not None or geo_cancelled:
        mentioned.add(Dimension.GEO)
    if geo_cancelled:
        cancelled.add(Dimension.GEO)
    if explicit_zones:
        mentioned.add(Dimension.ZONE)

    # Скасування — теж зрозумілий намір, а не «нічого не зрозуміло». Прохання
    # показати альтернативи, перелік баз чи слово-підсумок — теж намір (навіть
    # без інших фільтрів).
    understood = bool(mentioned or section_named or request_marker or both_bases or want_total)

    return ParsedQuery(
        query=query,
        understood=understood,
        section_named=section_named,
        mentioned=frozenset(mentioned),
        cancelled=frozenset(cancelled),
        request_marker=request_marker,
        both_bases=both_bases,
        want_total=want_total,
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
