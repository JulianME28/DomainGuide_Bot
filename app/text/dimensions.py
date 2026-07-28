"""Єдиний механізм розбору вимірів запиту.

═══════════════════════════════════════════════════════════════════════════
НАВІЩО ЦЕЙ МОДУЛЬ
═══════════════════════════════════════════════════════════════════════════

Раніше кожен вимір розбирався по-своєму: країна й мова — через список
готових фраз, метрики — через окрему перевірку сусідніх слів. Через це
той самий баг довелося ловити двічі: спершу для DR, потім для трафіку.

    «будь-яка країна др від 50»     ← слово «будь-яка» від КРАЇНИ
    «будь-яка країна трафік від 50»   вимикало сусідню метрику

Тут усі виміри описані ОДНАКОВО, одним переліком SPECS, і розбираються
одним кодом. Додати новий вимір (вихідні лінки, заспамленість) — це
дописати рядок у SPECS. Правила застосуються до нього автоматично, тому
повторити цей баг для наступного виміру вже не вийде.

═══════════════════════════════════════════════════════════════════════════
ТРИ ПРАВИЛА, ОДНАКОВІ ДЛЯ ВСІХ ВИМІРІВ
═══════════════════════════════════════════════════════════════════════════

1. ВИМІР ВОЛОДІЄ СВОЇМ ШМАТКОМ ТЕКСТУ.
   Шматок починається на назві виміру й закінчується там, де починається
   назва НАСТУПНОГО виміру. У запиті

       трафік від 50 др від 20
       └── трафік ──┘└── DR ──┘

   кожна метрика читає лише свій шматок і фізично не бачить чужого.
   Саме це й ламалося: раніше межа проходила по комі, тож без коми
   виміри читали текст один одного.

2. ЧИСЛО СИЛЬНІШЕ ЗА СЛОВО.
   Спершу шукається число, і лише якщо його немає — слова скасування.
   Явно вказане «від 50» завжди перемагає розпливчасте «будь-який».

3. СКАСУВАННЯ ЗАБИРАЄ СВІЙ ТЕКСТ ІЗ СОБОЮ.
   Знайдена фраза «будь-яка країна» затирається пробілами, тому вплинути
   на сусідів вона вже не може — ні на числа, ні на пошук країни в словнику.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.analytics.query import Dimension

# ---------------------------------------------------------------------------
# Слова скасування
# ---------------------------------------------------------------------------

# Стоять ПЕРЕД назвою виміру: «будь-яка країна», «всі мови», «без урахування».
_MARKER_BEFORE = (
    r"(?:будь[-\s]?як\w*|всі\w*|усі\w*|без\s+урахування|без\s+огляду\s+на"
    r"|незалежно\s+від|не\s+враховувати|не\s+залежно\s+від|any|all|без)"
)

# Стоять ПІСЛЯ назви виміру: «DR не важливий», «трафік будь-який».
_MARKER_AFTER = (
    r"(?:не\s+важлив\w*|не\s+важн\w*|неважлив\w*|без\s+обмеж\w*|будь[-\s]?як\w*"
    r"|байдуж\w*|не\s+має\s+значен\w*|не\s+потріб\w*|не\s+цікав\w*|any)"
)

# Дрібні слова, які можуть стояти між словом скасування й назвою виміру:
# «без урахування ПО країні».
_FILLER = r"(?:по|на|щодо|у|в|до|з)\s+"

# Число: підтримує «1 200», «1,200», «1.5k».
_NUMBER = r"(\d[\d\s.,]*\s*[kкmм]?)"

# Оператори порогів. Напрям («від» = мінімум, «до» = максимум) визначається
# СЛОВОМ, а не позицією, тому «DR від 50 і трафік від 50» дає два мінімуми, а не
# інвертує один із них.
_MIN_OP = r"від|понад|мінімум|більш\w*|более|more\s+than|from|>=?"
_MAX_OP = r"до|максимум|менш\w*|<=?"

# Один поріг: необов'язкове заперечення «не» + оператор + число. Заперечення
# ІНВЕРТУЄ напрям: «не менше 50» = мінімум 50, «не більше 50» = максимум 50.
# «не» разом з оператором з'їдаються одним збігом, тож голе «менше» всередині
# «не менше» окремо вже не матчиться (раніше саме через це виходила інверсія).
_THRESHOLD = re.compile(
    rf"(?P<neg>\bне\s+)?(?P<op>{_MIN_OP}|{_MAX_OP})\s*(?P<num>\d[\d\s.,]*\s*[kкmм]?)"
)

# За якими словами оператор означає МІНІМУM (решта — максимум).
_MIN_STARTS = ("від", "понад", "мінім", "більш", "более", "more", "from", ">")


def _is_minimum(op: str, negated: bool) -> bool:
    """Чи це поріг-мінімум. Заперечення «не» перевертає напрям оператора."""
    op = op.strip().lower()
    minimum = any(op.startswith(word) for word in _MIN_STARTS)
    return (not minimum) if negated else minimum


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """Опис одного виміру: як його називають і чи має він числа."""

    dimension: str
    stems: tuple[str, ...]
    """Незмінні початки слів, якими вимір називають у запиті.

    Саме початки, а не цілі слова: «країн» ловить і «країна», і «країни»,
    і «країнах». Перед стемом завжди вимагається межа слова, інакше «мов»
    знаходилося б усередині «англомовні».
    """

    numeric: bool
    """Чи має вимір числовий діапазон («від 50 до 100»)."""

    active: bool = True
    """False — вимір поки не має колонки в даних.

    Такі виміри все одно розбираються: їхні фрази скасування мають бути
    впізнані й затерті, щоб не заважати сусідам. Просто у запит вони поки
    нікуди не потрапляють.
    """

    bare_is_max: bool = False
    """Куди йде число БЕЗ слова напрямку («заспамленість 20», «20 вихідних»).

    Для DR і трафіку більше = краще, тож голе число — це МІНІМУМ («трафік 100»
    = ≥100). Для заспамленості й вихідних лінків менше = краще, тож голе число
    — це МАКСИМУМ («заспамленість 20» = ≤20). Явні «від»/«до» сильніші за це
    замовчування завжди.
    """


# ---------------------------------------------------------------------------
# ПЕРЕЛІК ВИМІРІВ — єдине місце, куди дописувати новий
# ---------------------------------------------------------------------------
SPECS: tuple[DimensionSpec, ...] = (
    DimensionSpec(
        Dimension.COUNTRY,
        # «гео» більше НЕ синонім країни — це окремий фільтр по колонці GEO
        # (див. freeform._extract_geo). Тут лишаються тільки назви країни.
        stems=("країн", "country", "countries"),
        numeric=False,
    ),
    DimensionSpec(
        Dimension.LANGUAGE,
        stems=("мов", "language", "languages"),
        numeric=False,
    ),
    # Запит лише по доменній зоні. Саме ЗНАЧЕННЯ («зона .co.uk») дістає
    # freeform._extract_zone — тут вимір потрібен, щоб працювали спільні
    # фрази скасування («зона не важлива», «будь-яка зона»).
    DimensionSpec(
        Dimension.ZONE,
        stems=("зон", "zone"),
        numeric=False,
    ),
    DimensionSpec(
        Dimension.TRAFFIC,
        stems=("трафік", "трафик", "traffic", "відвідуван"),
        numeric=True,
    ),
    DimensionSpec(
        Dimension.DR,
        stems=("dr", "др", "рейтинг домену"),
        numeric=True,
    ),
    # --- аналіз заспамленості: підключений для «Морд» ---
    # Тут менше = краще, тож голе число без слова напрямку — це МАКСИМУМ («до N»).
    #
    # ВАЖЛИВО: слова про «вихідні лінки» ведуть на ТОЙ САМИЙ фільтр заспамленості
    # (стовпець G), що й слово «заспамленість». Стовпець F («вихідні») числом не
    # фільтрується — він лише службовий відсів мертвих сайтів у двигуні. Тож
    # «до 20 вихідних» = «заспамленість до 20» = G ≤ 20.
    DimensionSpec(
        Dimension.SPAM,
        stems=(
            "заспамлен",
            "спамн",
            "спам",
            "вихідних лінк",
            "вихідні лінк",
            "вихідними лінк",
            "вихідних посилан",
            "вихідні посилан",
            "вихідних",
            "outlink",
        ),
        numeric=True,
        bare_is_max=True,
    ),
)


def _stem_pattern(stems: tuple[str, ...]) -> str:
    """Збирає з переліку стемів шматок регулярки: \\b(?:країн|гео)\\w*."""
    alternatives = "|".join(re.escape(stem) for stem in sorted(stems, key=len, reverse=True))
    return rf"\b(?:{alternatives})\w*"


@dataclass(frozen=True, slots=True)
class _CompiledSpec:
    """Готові до пошуку регулярки одного виміру."""

    spec: DimensionSpec
    keyword: re.Pattern[str]
    cancel_before: re.Pattern[str]
    cancel_after: re.Pattern[str]


def _compile(spec: DimensionSpec) -> _CompiledSpec:
    stem = _stem_pattern(spec.stems)
    return _CompiledSpec(
        spec=spec,
        keyword=re.compile(stem),
        # «будь-яка країна», «всі мови», «без урахування по країні»
        cancel_before=re.compile(rf"{_MARKER_BEFORE}\s+(?:{_FILLER})?{stem}"),
        # «країна не важлива», «DR будь-який», «dr зовсім не важливий»
        cancel_after=re.compile(rf"{stem}\W+(?:\w+\s+){{0,1}}{_MARKER_AFTER}"),
    )


_COMPILED: tuple[_CompiledSpec, ...] = tuple(_compile(spec) for spec in SPECS)

# Одна регулярка з назвами ВСІХ вимірів — нею шукається межа шматка.
_ANY_KEYWORD = re.compile("|".join(_stem_pattern(spec.stems) for spec in SPECS))


@dataclass(frozen=True, slots=True)
class DimensionMatch:
    """Що знайдено про один вимір."""

    dimension: str
    cancelled: bool = False
    minimum: float | None = None
    maximum: float | None = None

    @property
    def has_value(self) -> bool:
        return self.minimum is not None or self.maximum is not None


def _to_number(raw: str) -> float | None:
    from app.data.parsing import parse_number

    return parse_number(raw)


def _window(text: str, start: int, keyword_end: int) -> tuple[int, int]:
    """Межі шматка тексту, що належить виміру.

    Шматок тягнеться від назви виміру до назви НАСТУПНОГО виміру. Це і є
    правило №1: сусідні виміри не бачать тексту один одного.
    """
    following = _ANY_KEYWORD.search(text, keyword_end)
    return start, following.start() if following else len(text)


def _read_lead_number(lead: str, *, bare_is_max: bool) -> tuple[float | None, float | None] | None:
    """Провідне число ПЕРЕД назвою виміру: «20 вихідних», «до 20 вихідних».

    Викликається лише коли вимір ПЕРШИЙ у тексті — тоді число зліва точно його,
    а не чужого сусіда. Беремо поріг/число, найближчий до назви (у кінці lead).
    """
    thresholds = list(_THRESHOLD.finditer(lead))
    if thresholds:
        match = thresholds[-1]
        value = _to_number(match.group("num"))
        if value is not None:
            if _is_minimum(match.group("op"), match.group("neg") is not None):
                return value, None
            return None, value
    numbers = list(re.finditer(_NUMBER, lead))
    if numbers:
        value = _to_number(numbers[-1].group(1))
        if value is not None:
            return (None, value) if bare_is_max else (value, None)
    return None


def _read_numbers(
    window: str, *, bare_is_max: bool = False
) -> tuple[float | None, float | None] | None:
    """Читає «від N до M», «від N», «до M» або просто число після назви.

    Кожен поріг класифікується за СВОЇМ словом-оператором (з урахуванням
    заперечення «не»), тому «від» завжди мінімум, «до» завжди максимум, а два
    «від» в одному запиті дають два мінімуми — інверсії бути не може.

    `bare_is_max` — куди йде число БЕЗ слова напрямку: для заспамленості й
    вихідних лінків це максимум («заспамленість 20» = ≤20), для DR/трафіку —
    мінімум («трафік 100» = ≥100).
    """
    both = re.search(rf"від\s*{_NUMBER}\s*до\s*{_NUMBER}", window)
    if both:
        return _to_number(both.group(1)), _to_number(both.group(2))

    minimum: float | None = None
    maximum: float | None = None
    for match in _THRESHOLD.finditer(window):
        value = _to_number(match.group("num"))
        if value is None:
            continue
        if _is_minimum(match.group("op"), match.group("neg") is not None):
            if minimum is None:
                minimum = value
        elif maximum is None:
            maximum = value
    if minimum is not None or maximum is not None:
        return minimum, maximum

    # «трафік 100» — назва й одразу число, без «від». Напрямок за замовчуванням
    # залежить від виміру: DR/трафік → мінімум, заспамленість/вихідні → максимум.
    bare = re.search(rf"^\W*\w*\W{{0,3}}{_NUMBER}", window)
    if bare:
        value = _to_number(bare.group(1))
        return (None, value) if bare_is_max else (value, None)

    return None


def resolve_dimensions(text: str) -> tuple[dict[str, DimensionMatch], str]:
    """Розбирає всі виміри одним проходом.

    Повертає (що знайдено по кожному виміру, текст без фраз скасування).

    Що затирається, а що ні:
      * фрази скасування («будь-яка країна») — ЗАТИРАЮТЬСЯ. Інакше словник
        сплутав би їх із назвою країни, а слово «будь-який» вплинуло б на
        сусідню метрику;
      * числа метрик («трафік від 50») — НЕ затираються. Число нікому не
        заважає: воно не назва країни, а кожна метрика читає лише СВОЄ
        вікно, тож і сусіда не зачепить.

    Чому не можна затирати числові вікна. Вікно метрики тягнеться до
    наступної метрики, і між ними може стояти країна чи мова:

        трафік від 50 Німеччина др від 20
                      └── ось вона ──┘

    Затерши все вікно трафіку, ми з'їли б «Німеччину», і країна тихо
    зникла б із запиту. Тому затираємо лише те, що справді треба сховати
    від словника, — фрази скасування.
    """
    found: dict[str, DimensionMatch] = {}
    cancel_spans: list[tuple[int, int]] = []

    for compiled in _COMPILED:
        spec = compiled.spec
        keyword = compiled.keyword.search(text)

        # ПРАВИЛО 2: спершу число (читаємо зі свого вікна, але НЕ затираємо).
        if keyword is not None and spec.numeric:
            start, end = _window(text, keyword.start(), keyword.end())
            numbers = _read_numbers(text[start:end], bare_is_max=spec.bare_is_max)
            # Число ПЕРЕД назвою («20 вихідних», «до 20 вихідних») — тільки для
            # заспамленості/вихідних (bare_is_max) і лише коли вимір ПЕРШИЙ у
            # тексті, інакше воно належить сусідові зліва. Для DR/трафіку діє
            # давнє правило: число перед назвою — не фільтр.
            if (
                numbers is None
                and spec.bare_is_max
                and not _ANY_KEYWORD.search(text[: keyword.start()])
            ):
                numbers = _read_lead_number(text[: keyword.start()], bare_is_max=spec.bare_is_max)
            if numbers is not None:
                found[spec.dimension] = DimensionMatch(spec.dimension, False, *numbers)
                continue

        # Числа немає — шукаємо скасування (обидва порядки слів).
        cancel = compiled.cancel_before.search(text) or compiled.cancel_after.search(text)
        if cancel is not None:
            found[spec.dimension] = DimensionMatch(spec.dimension, cancelled=True)
            cancel_spans.append(cancel.span())

    # ПРАВИЛО 3: ховаємо від словника лише фрази скасування.
    cleaned = text
    for start, end in cancel_spans:
        cleaned = cleaned[:start] + " " * (end - start) + cleaned[end:]

    return found, cleaned


def active_dimensions() -> frozenset[str]:
    """Виміри, які реально потрапляють у запит."""
    return frozenset(spec.dimension for spec in SPECS if spec.active)
