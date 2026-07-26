"""Розпізнавання країн і мов у тексті користувача.

Два окремі резолвери — для країни й для мови. Це не примха: країна й мова
в цьому проєкті означають різні речі й дають різні відповіді, тому змішувати
їх в одну функцію не можна.

    «Німеччина», «.de»  → КРАЇНА  → рахуємо за доменною зоною
    «німецькою»          → МОВА    → рахуємо за колонкою мови

ГОЛОВНА ХИТРІСТЬ ЦЬОГО МОДУЛЯ — порядок розпізнавання.

Українські назви країн і мов часто починаються однаково:

    «Англія»    і  «англійською»    → обидва починаються на «англі»
    «Італія»    і  «італійською»    → обидва починаються на «італі»
    «Латвія»    і  «латвійською»    → обидва починаються на «латві»
    «Україна»   і  «українською»    → обидва починаються на «україн»

Тому спершу шукаємо МОВУ, потім «затираємо» знайдений шматок тексту
пробілами — і лише після цього шукаємо країну. Так слово «англійською»
вже не може перетворитися на країну Англія.

Ще одне правило: у вільному тексті НЕ використовуються голі двобуквені коди.
«in», «is», «no», «it» — це звичайні англійські слова, і вони давали б купу
хибних збігів. Але зона з крапкою («.de», «.it») однозначна й тому дозволена.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.dictionary.countries import (
    COUNTRIES,
    SAFE_SHORT_COUNTRY_CODES,
    Country,
    countries_with_language,
    country_by_code,
    country_by_zone,
)
from app.dictionary.languages import LANGUAGES, Language
from app.dictionary.normalize import find_zone_mentions, mask_span, normalize_text
from app.dictionary.zones import is_global_zone

_WORD_WITH_SPAN = re.compile(r"[0-9a-zа-яёєіїґ]+")


@dataclass(frozen=True, slots=True)
class Match:
    """Де саме в тексті знайдено збіг і наскільки він «вагомий»."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class EntityScan:
    """Що вдалося впізнати у вільному запиті."""

    country: Country | None = None
    language: Language | None = None
    zones: tuple[str, ...] = ()
    """Явно згадані зони країн: користувач написав «.de»."""

    global_zones: tuple[str, ...] = ()
    """Явно згадані глобальні зони: «.com», «.net». Нікому не належать."""

    @property
    def is_empty(self) -> bool:
        return not (self.country or self.language or self.zones or self.global_zones)


def _tokens_with_spans(text: str) -> list[tuple[str, int, int]]:
    """Слова тексту разом із позиціями — позиції потрібні для «затирання»."""
    return [(m.group(0), m.start(), m.end()) for m in _WORD_WITH_SPAN.finditer(text)]


def _match_phrases(text: str, phrases: tuple[str, ...]) -> Match | None:
    """Шукає багатослівні назви: «united kingdom», «південна африка»."""
    best: Match | None = None
    for phrase in phrases:
        if not phrase:
            continue
        position = text.find(phrase)
        while position != -1:
            start, end = position, position + len(phrase)
            # Перевіряємо, що це окрема фраза, а не шматок довшого слова.
            before_ok = start == 0 or not text[start - 1].isalnum()
            after_ok = end == len(text) or not text[end].isalnum()
            if before_ok and after_ok:
                candidate = Match(start, end)
                if best is None or candidate.length > best.length:
                    best = candidate
                break
            position = text.find(phrase, position + 1)
    return best


def _match_tokens(
    tokens: list[tuple[str, int, int]],
    exact: frozenset[str] | tuple[str, ...],
    stems: tuple[str, ...],
    *,
    short_codes: tuple[str, ...] = (),
    allow_short: bool = False,
) -> Match | None:
    """Шукає збіг серед окремих слів тексту.

    Спершу точні збіги слів, потім збіги за початком слова (стеми) —
    саме вони ловлять українські відмінки.
    """
    best: Match | None = None

    for token, start, end in tokens:
        matched = token in exact
        if not matched and allow_short and token in short_codes:
            matched = True
        if not matched:
            matched = any(stem and token.startswith(stem) for stem in stems)

        if matched:
            candidate = Match(start, end)
            if best is None or candidate.length > best.length:
                best = candidate

    return best


# ---------------------------------------------------------------------------
# Резолвер МОВИ
# ---------------------------------------------------------------------------


def find_language_match(text: str, *, allow_short: bool = False) -> tuple[Language, Match] | None:
    """Шукає мову в тексті. Повертає мову і місце, де її знайдено.

    allow_short=True дозволяє короткі коди («fr»). Це доречно, коли бот уже
    спитав саме про мову і користувач відповідає одним словом. У вільному
    тексті короткі коди вимкнені — вони дають хибні збіги.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None

    tokens = _tokens_with_spans(normalized)
    best: tuple[Language, Match] | None = None

    for language in LANGUAGES.values():
        match = _match_tokens(
            tokens,
            language.synonyms,
            language.stems_uk,
            short_codes=(language.code,),
            allow_short=allow_short,
        )
        if match is None:
            continue
        if best is None or match.length > best[1].length:
            best = (language, match)

    return best


def resolve_language(text: str, *, allow_short: bool = False) -> Language | None:
    """Мова за текстом користувача. None — не впізнали."""
    found = find_language_match(text, allow_short=allow_short)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Резолвер КРАЇНИ
# ---------------------------------------------------------------------------


def find_country_match(text: str, *, allow_short: bool = False) -> tuple[Country, Match] | None:
    """Шукає країну в тексті: за назвою, синонімом або доменною зоною."""
    normalized = normalize_text(text)
    if not normalized:
        return None

    # Явна зона («.de») — найнадійніший сигнал, тому перевіряємо її першою.
    for zone, start, end in find_zone_mentions(normalized):
        country = country_by_zone(zone)
        if country is not None:
            return country, Match(start, end)

    tokens = _tokens_with_spans(normalized)
    best: tuple[Country, Match] | None = None

    for country in COUNTRIES.values():
        match = _match_phrases(normalized, country.phrases)
        token_match = _match_tokens(
            tokens,
            country.synonyms | frozenset(country.exact_uk),
            country.stems_uk,
            short_codes=(country.code,),
            allow_short=allow_short,
        )
        if token_match is not None and (match is None or token_match.length > match.length):
            match = token_match

        if match is None:
            continue
        if best is None or match.length > best[1].length:
            best = (country, match)

    return best


def resolve_country(text: str, *, allow_short: bool = False) -> Country | None:
    """Країна за текстом користувача. None — не впізнали."""
    found = find_country_match(text, allow_short=allow_short)
    return found[0] if found else None


# Ознака, що текст — САМЕ перелік країн, а не суцільна проза: кома/плюс,
# сполучники-склеювачі списку, або слово «країн». Лише за такої ознаки (чи коли
# поруч уже є впізнані країни) вмикаємо короткі коди «uk»/«us» — інакше вони
# мовчать, щоб не ловити «in/is» у звичайному реченні.
_COUNTRY_LIST_SIGNAL = re.compile(r"[,+]|\bта\b|\bі\b|\bй\b|\bи\b|\bтакож\b|країн")


def _find_safe_short_country(text: str) -> tuple[Country, Match] | None:
    """Перший токен, що є безпечним коротким кодом країни («uk», «us», «uae»)."""
    for token, start, end in _tokens_with_spans(text):
        code = SAFE_SHORT_COUNTRY_CODES.get(token)
        if code is not None:
            country = country_by_code(code)
            if country is not None:
                return country, Match(start, end)
    return None


def find_all_countries(text: str, *, allow_short: bool = False) -> tuple[list[Country], str]:
    """Знаходить УСІ згадані країни — для списку в одному запиті.

    «Франция Индия Германия .fr» → [Франція, Індія, Німеччина] (по зоні теж).

    Повертає (унікальні країни в порядку появи, залишок тексту). Залишок —
    це той самий текст, де знайдені країни (і мови) затерті пробілами; із
    нього викликач дістає нерозпізнані слова-кандидати.

    Працює як `find_country_match`, тільки повторно: знайшли найкращий збіг —
    затерли його — шукаємо далі, поки збіги є. Спершу затираємо мови, щоб
    «англійською» не перетворилося на країну Англія (та сама хитрість, що в
    scan_entities, лише багаторазова).

    Наприкінці — окремий прохід по коротких кодах («uk», «us»), але ЛИШЕ коли
    контекст країновий (є ознака переліку або вже впізнані країни). Так «US» у
    списку не губиться, а «in/is» у прозі й далі не ловляться.
    """
    original = normalize_text(text)
    masked = original
    if not masked:
        return [], ""

    # Затираємо знайдений шматок і одразу нормалізуємо назад. Це важливо:
    # find_language_match / find_country_match всередині самі нормалізують
    # текст (стягують кілька пробілів в один), тому позиції їхніх збігів
    # рахуються від СТИСНУТОГО тексту. Якщо не стискати після кожного
    # затирання, наступний збіг ляже не туди.
    def blank(text_: str, start: int, end: int) -> str:
        return normalize_text(mask_span(text_, start, end))

    # 1) Прибираємо всі згадки мов — щоб вони не стали країнами.
    while True:
        language_found = find_language_match(masked, allow_short=allow_short)
        if language_found is None:
            break
        match = language_found[1]
        masked = blank(masked, match.start, match.end)

    # 2) Збираємо всі країни. Дедуп за кодом; порядок — за появою.
    countries: list[Country] = []
    seen: set[str] = set()
    # Стеля ітерацій — страховка від зациклення (тексту завжди коротшає).
    for _ in range(64):
        found = find_country_match(masked, allow_short=allow_short)
        if found is None:
            break
        country, match = found
        if country.code not in seen:
            seen.add(country.code)
            countries.append(country)
        masked = blank(masked, match.start, match.end)

    # 3) Короткі коди («uk», «us», «uae») — лише в країновому контексті: коли вже
    # є впізнані країни або текст явно виглядає як перелік. Поза цим — мовчать.
    context_ok = allow_short or bool(countries) or _COUNTRY_LIST_SIGNAL.search(original) is not None
    if context_ok:
        for _ in range(64):
            found = _find_safe_short_country(masked)
            if found is None:
                break
            country, match = found
            if country.code not in seen:
                seen.add(country.code)
                countries.append(country)
            masked = blank(masked, match.start, match.end)

    return countries, masked


# ---------------------------------------------------------------------------
# Повне сканування вільного тексту
# ---------------------------------------------------------------------------


def scan_entities(text: str) -> EntityScan:
    """Розбирає вільний запит: що тут країна, що мова, а що доменна зона.

    Порядок принциповий (пояснення на початку файлу):
      1. мова — і одразу «затираємо» знайдене слово;
      2. країна — вже по затертому тексту;
      3. явні зони — вони однозначні й шукаються окремо.
    """
    normalized = normalize_text(text)
    if not normalized:
        return EntityScan()

    # Крок 1: мова. Короткі коди вимкнені — це вільний текст.
    language_found = find_language_match(normalized, allow_short=False)
    language = language_found[0] if language_found else None

    # Крок 2: затираємо слово мови, щоб воно не перетворилося на країну.
    text_for_country = normalized
    if language_found is not None:
        match = language_found[1]
        text_for_country = mask_span(normalized, match.start, match.end)

    country_found = find_country_match(text_for_country, allow_short=False)
    country = country_found[0] if country_found else None

    # Крок 3: явні зони. Розділяємо на «країнні» й глобальні.
    country_zones: list[str] = []
    global_zones: list[str] = []
    for zone, _start, _end in find_zone_mentions(normalized):
        if is_global_zone(zone):
            global_zones.append(zone)
        elif country_by_zone(zone) is not None:
            country_zones.append(zone)

    return EntityScan(
        country=country,
        language=language,
        zones=tuple(dict.fromkeys(country_zones)),
        global_zones=tuple(dict.fromkeys(global_zones)),
    )


# ---------------------------------------------------------------------------
# Підказка «ви переплутали режим»
#
# Мовний і країновий запити — різні речі (див. початок файлу). Коли в мовному
# режимі вводять «.ua» або «Німеччину», відповідь була б порожня: доменна зона
# не може бути мовою. Замість мовчазного нуля показуємо підказку з двома
# варіантами. Дзеркально — коли в країновому режимі вводять назву мови.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrossModeHint:
    """Що ввели «не в тому режимі» і які два варіанти можна запропонувати.

    Обидва поля описують ОДНЕ введення з двох боків: `country` — країновий
    варіант, `language` — мовний. У країновому режимі для мов, якими пишуть
    кілька країн (німецька, англійська), однозначної країни немає — тоді
    `country` буде None, і кнопку країни заміняє звичайний вибір країни.
    """

    query_text: str
    """Те, що ввів користувач, — для цитати в підказці."""

    country: Country | None
    language: Language | None
    via_zone: bool = False
    """True, якщо країну впізнали з доменної зони («.ua»), а не з назви."""


def _mentions_country_zone(text: str) -> bool:
    """Чи є в тексті доменна зона, закріплена за країною («.ua», «.de»)."""
    normalized = normalize_text(text)
    return any(
        country_by_zone(zone) is not None for zone, _start, _end in find_zone_mentions(normalized)
    )


def hint_for_language_mode(text: str) -> CrossModeHint | None:
    """Мовний режим: якщо введене — країна чи доменна зона, а не мова.

    Викликати лише ПІСЛЯ того, як мову розпізнати не вдалося. Якщо текст —
    країна, повертаємо підказку з двома варіантами (країна та її основна мова).
    Якщо це не країна (наприклад, глобальна зона «.com») — None, і далі йде
    звичайне повідомлення «не впізнав мову».
    """
    found = find_country_match(text, allow_short=True)
    if found is None:
        return None

    country = found[0]
    return CrossModeHint(
        query_text=text.strip(),
        country=country,
        language=country.language,
        via_zone=_mentions_country_zone(text),
    )


def hint_for_country_mode(text: str) -> CrossModeHint | None:
    """Країновий режим: якщо введене — мова, а не країна («українською»).

    Повертає підказку з двома варіантами. Для однозначних мов (українська →
    Україна) є конкретна країна; для мов кількох країн (німецька) country=None,
    і кнопку країни заміняє загальний вибір.
    """
    entities = scan_entities(text)
    if entities.language is None or entities.country is not None:
        return None

    homes = countries_with_language(entities.language.code)
    return CrossModeHint(
        query_text=text.strip(),
        country=homes[0] if len(homes) == 1 else None,
        language=entities.language,
    )
