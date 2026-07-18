"""Словник мов.

У таблиці мова записана англійською назвою: English, Spanish, Turkish...
А користувач може написати як завгодно: «німецькою», «German», «англомовні».
Цей модуль зшиває одне з одним.

Про поле `widespread` (спільні мови). Англійською, іспанською, португальською
та арабською пишуть у багатьох країнах. Тому «англомовний донор» зовсім не
означає «донор із Британії» — і бот про це чесно попереджає. Для однозначних
мов (French, German) попередження не потрібне.

Про «стеми». Українська мова відмінюється: французька, французької,
французькою, французькі. Замість того щоб перелічувати всі форми, ми
зберігаємо незмінний початок слова — «французьк» — і порівнюємо за ним.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from app.dictionary.normalize import normalize_text


@dataclass(frozen=True, slots=True)
class Language:
    """Одна мова з усіма способами її назвати."""

    code: str
    """Короткий код: "de", "en". Усередині бота мова позначається саме так."""

    name_en: str
    """Англійська назва — рівно так, як мова записана в таблиці."""

    name_uk: str
    """Українська назва в називному відмінку: «німецька»."""

    instrumental_uk: str
    """Орудний відмінок: «німецькою». Для рядка «N донорів німецькою мовою»."""

    widespread: bool
    """True для мов, якими пишуть багато країн (en, es, pt, ar)."""

    data_keys: frozenset[str]
    """Як мова може бути записана в таблиці (у нормалізованому вигляді)."""

    synonyms: frozenset[str]
    """Слова-цілком, за якими мову можна впізнати в запиті користувача."""

    stems_uk: tuple[str, ...]
    """Незмінні початки українських слів: «німецьк», «німецькомов»."""


# ---------------------------------------------------------------------------
# Таблиця мов.
#
# Порядок полів:
#   код, назва в таблиці, українська назва, орудний відмінок,
#   спільна мова?, українські стеми, додаткові синоніми
#
# Спільні мови (True в п'ятій колонці) — це en, es, pt, ar. Саме для них
# бот показує попередження «цією мовою пишуть багато країн».
# ---------------------------------------------------------------------------
_RAW: tuple[tuple[str, str, str, str, bool, tuple[str, ...], tuple[str, ...]], ...] = (
    # --- найпоширеніші в базі ---
    ("en", "English", "англійська", "англійською", True,
     ("англійськ", "англомов"), ("english", "eng")),
    ("es", "Spanish", "іспанська", "іспанською", True,
     ("іспансь", "іспаномов"), ("spanish", "espanol", "castellano")),
    ("pt", "Portuguese", "португальська", "португальською", True,
     ("португальс", "португаломов"), ("portuguese", "portugues")),
    ("ar", "Arabic", "арабська", "арабською", True,
     ("арабськ", "арабомов"), ("arabic",)),
    ("de", "German", "німецька", "німецькою", False,
     ("німецьк", "німецькомов"), ("german", "deutsch")),
    ("fr", "French", "французька", "французькою", False,
     ("французьк", "французькомов", "франкомов"), ("french", "francais")),
    ("it", "Italian", "італійська", "італійською", False,
     ("італійськ", "італомов"), ("italian", "italiano")),
    ("tr", "Turkish", "турецька", "турецькою", False,
     ("турецьк", "туркомов"), ("turkish", "turkce")),
    ("nl", "Dutch", "нідерландська", "нідерландською", False,
     ("нідерландськ", "голландськ"), ("dutch", "nederlands")),
    ("pl", "Polish", "польська", "польською", False,
     ("польськ", "полькомов"), ("polish", "polski")),
    ("ro", "Romanian", "румунська", "румунською", False,
     ("румунськ",), ("romanian", "romana")),
    ("el", "Greek", "грецька", "грецькою", False,
     ("грецьк",), ("greek", "hellenic")),
    ("th", "Thai", "тайська", "тайською", False,
     ("тайськ", "таїландськ"), ("thai",)),
    ("hi", "Hindi", "гінді", "мовою гінді", False,
     ("гінді", "хінді"), ("hindi",)),
    ("zh", "Chinese", "китайська", "китайською", False,
     ("китайськ",), ("chinese", "mandarin", "chinese simplified", "chinese traditional")),
    ("ja", "Japanese", "японська", "японською", False,
     ("японськ",), ("japanese",)),
    ("ru", "Russian", "російська", "російською", False,
     ("російськ", "російськомов"), ("russian",)),
    ("hu", "Hungarian", "угорська", "угорською", False,
     ("угорськ",), ("hungarian", "magyar")),
    ("sv", "Swedish", "шведська", "шведською", False,
     ("шведськ",), ("swedish", "svenska")),
    ("fa", "Persian", "перська", "перською", False,
     ("перськ", "фарсі"), ("persian", "farsi")),
    ("ko", "Korean", "корейська", "корейською", False,
     ("корейськ",), ("korean",)),
    ("cs", "Czech", "чеська", "чеською", False,
     ("чеськ",), ("czech", "cestina")),
    ("da", "Danish", "данська", "данською", False,
     ("данськ", "датськ"), ("danish", "dansk")),
    ("hr", "Croatian", "хорватська", "хорватською", False,
     ("хорватськ",), ("croatian", "hrvatski")),
    ("bg", "Bulgarian", "болгарська", "болгарською", False,
     ("болгарськ",), ("bulgarian",)),
    ("sr", "Serbian", "сербська", "сербською", False,
     ("сербськ",), ("serbian", "srpski")),
    ("uk", "Ukrainian", "українська", "українською", False,
     ("українськ", "україномов"), ("ukrainian",)),
    ("no", "Norwegian", "норвезька", "норвезькою", False,
     ("норвезьк",), ("norwegian", "norsk", "bokmal")),
    ("lt", "Lithuanian", "литовська", "литовською", False,
     ("литовськ",), ("lithuanian",)),
    ("sk", "Slovak", "словацька", "словацькою", False,
     ("словацьк",), ("slovak",)),
    ("he", "Hebrew", "іврит", "івритом", False,
     ("іврит", "єврейськ"), ("hebrew", "ivrit")),
    ("sl", "Slovenian", "словенська", "словенською", False,
     ("словенськ",), ("slovenian", "slovene")),
    ("lv", "Latvian", "латвійська", "латвійською", False,
     ("латвійськ", "латиськ"), ("latvian",)),
    ("bs", "Bosnian", "боснійська", "боснійською", False,
     ("боснійськ",), ("bosnian",)),
    ("et", "Estonian", "естонська", "естонською", False,
     ("естонськ",), ("estonian",)),
    ("fi", "Finnish", "фінська", "фінською", False,
     ("фінськ",), ("finnish", "suomi")),
    ("ms", "Malay", "малайська", "малайською", False,
     ("малайськ",), ("malay", "melayu")),
    ("ka", "Georgian", "грузинська", "грузинською", False,
     ("грузинськ",), ("georgian",)),
    ("ca", "Catalan", "каталонська", "каталонською", False,
     ("каталонськ",), ("catalan", "catala")),
    ("id", "Indonesian", "індонезійська", "індонезійською", False,
     ("індонезійськ",), ("indonesian", "bahasa")),
    ("vi", "Vietnamese", "вʼєтнамська", "вʼєтнамською", False,
     ("вєтнамськ",), ("vietnamese",)),
    # --- дрібніші мови ---
    ("sq", "Albanian", "албанська", "албанською", False, ("албанськ",), ("albanian",)),
    ("mk", "Macedonian", "македонська", "македонською", False, ("македонськ",), ("macedonian",)),
    ("hy", "Armenian", "вірменська", "вірменською", False, ("вірменськ",), ("armenian",)),
    ("az", "Azerbaijani", "азербайджанська", "азербайджанською", False,
     ("азербайджанськ",), ("azerbaijani", "azeri")),
    ("kk", "Kazakh", "казахська", "казахською", False, ("казахськ",), ("kazakh",)),
    ("ur", "Urdu", "урду", "мовою урду", False, ("урду",), ("urdu",)),
    ("bn", "Bengali", "бенгальська", "бенгальською", False,
     ("бенгальськ",), ("bengali", "bangla")),
    ("ta", "Tamil", "тамільська", "тамільською", False, ("тамільськ",), ("tamil",)),
    ("te", "Telugu", "телугу", "мовою телугу", False, ("телугу",), ("telugu",)),
    ("mr", "Marathi", "маратхі", "мовою маратхі", False, ("маратхі",), ("marathi",)),
    ("pa", "Punjabi", "панджабі", "мовою панджабі", False, ("панджабі",), ("punjabi",)),
    ("gu", "Gujarati", "гуджараті", "мовою гуджараті", False, ("гуджараті",), ("gujarati",)),
    ("tl", "Filipino", "філіппінська", "філіппінською", False,
     ("філіппінськ", "філіпінськ"), ("filipino", "tagalog")),
    ("sw", "Swahili", "суахілі", "мовою суахілі", False, ("суахілі",), ("swahili",)),
    ("af", "Afrikaans", "африкаанс", "мовою африкаанс", False, ("африкаанс",), ("afrikaans",)),
    ("is", "Icelandic", "ісландська", "ісландською", False, ("ісландськ",), ("icelandic",)),
    ("ga", "Irish", "ірландська", "ірландською", False, ("ірландськомов",), ("irish", "gaeilge")),
    ("cy", "Welsh", "валлійська", "валлійською", False, ("валлійськ",), ("welsh",)),
    ("mt", "Maltese", "мальтійська", "мальтійською", False, ("мальтійськ",), ("maltese",)),
    ("be", "Belarusian", "білоруська", "білоруською", False,
     ("білоруськ",), ("belarusian", "belarussian")),
    ("mn", "Mongolian", "монгольська", "монгольською", False, ("монгольськ",), ("mongolian",)),
    ("ne", "Nepali", "непальська", "непальською", False, ("непальськ",), ("nepali",)),
    ("si", "Sinhala", "сингальська", "сингальською", False, ("сингальськ",), ("sinhala",)),
    ("km", "Khmer", "кхмерська", "кхмерською", False, ("кхмерськ",), ("khmer",)),
    ("lo", "Lao", "лаоська", "лаоською", False, ("лаоськ",), ("lao",)),
    ("my", "Burmese", "бірманська", "бірманською", False, ("бірманськ",), ("burmese", "myanmar")),
    ("am", "Amharic", "амхарська", "амхарською", False, ("амхарськ",), ("amharic",)),
    ("so", "Somali", "сомалійська", "сомалійською", False, ("сомалійськ",), ("somali",)),
    ("ku", "Kurdish", "курдська", "курдською", False, ("курдськ",), ("kurdish",)),
    ("uz", "Uzbek", "узбецька", "узбецькою", False, ("узбецьк",), ("uzbek",)),
    ("gl", "Galician", "галісійська", "галісійською", False, ("галісійськ",), ("galician",)),
    ("eu", "Basque", "баскська", "баскською", False, ("баскськ",), ("basque",)),
    ("lb", "Luxembourgish", "люксембурзька", "люксембурзькою", False,
     ("люксембурзьк",), ("luxembourgish",)),
)  # fmt: skip


def _build() -> dict[str, Language]:
    """Складає таблицю вище в готові об'єкти Language."""
    languages: dict[str, Language] = {}

    for code, name_en, name_uk, instrumental, widespread, stems, extra in _RAW:
        # Синоніми, за якими мову впізнаємо в запиті: англійська назва,
        # українська назва, орудний відмінок і все додаткове з таблиці.
        synonyms = {normalize_text(name_en), normalize_text(name_uk), normalize_text(instrumental)}
        # У таблиці зустрічається один рядок, а тут може бути tuple або str —
        # рядок треба загорнути, інакше він розсиплеться на окремі літери.
        for item in (extra,) if isinstance(extra, str) else extra:
            synonyms.add(normalize_text(item))

        # Двобуквені синоніми прибираємо: "it", "in", "no", "is" збігаються
        # зі звичайними словами і дають хибні спрацювання у вільному тексті.
        synonyms = {s for s in synonyms if len(s) > 2}

        languages[code] = Language(
            code=code,
            name_en=name_en,
            name_uk=name_uk,
            instrumental_uk=instrumental,
            widespread=widespread,
            data_keys=frozenset({normalize_text(name_en)}),
            synonyms=frozenset(synonyms),
            stems_uk=tuple(normalize_text(s) for s in stems),
        )

    return languages


LANGUAGES: dict[str, Language] = MappingProxyType(_build())
"""Усі мови: код → Language."""

# Швидкий пошук за тим, як мова записана в таблиці: "german" → Language(de).
_BY_DATA_KEY: dict[str, Language] = MappingProxyType(
    {key: language for language in LANGUAGES.values() for key in language.data_keys}
)


def language_by_code(code: str) -> Language | None:
    """Мова за кодом: "de" → німецька."""
    return LANGUAGES.get(code)


def language_by_data_value(value: str) -> Language | None:
    """Мова за значенням із таблиці: "English " → англійська.

    Якщо мова в таблиці нам невідома — повертається None, і бот просто
    показує її як є. Нова мова в даних нічого не ламає.
    """
    return _BY_DATA_KEY.get(normalize_text(value))


def display_language(value: str) -> str:
    """Гарна назва мови для показу людині.

    Відома мова → «німецька». Невідома → як у таблиці, з великої літери.
    """
    language = language_by_data_value(value)
    if language is not None:
        return language.name_uk
    return value.strip().capitalize() if value.strip() else "не вказано"


WIDESPREAD_CODES: frozenset[str] = frozenset(
    code for code, language in LANGUAGES.items() if language.widespread
)
"""Коди мов, якими пишуть багато країн: en, es, pt, ar."""
