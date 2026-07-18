"""Словник країн із їхніми СПРАВЖНІМИ доменними зонами.

Це ядро моделі гео. Колонки країни в даних немає, тому країна визначається
через доменну зону: сайт у зоні .de вважається німецьким.

Два принципи, яких тут дотримано суворо:

1. Тільки справжні ccTLD. Зона .de належить Німеччині, .fr — Франції.
   А .com, .net, .online не належать нікому — вони в zones.py і жодній
   країні не приписуються.

2. Основна мова країни — це окреме поле. Саме воно дає «мовний додаток»:
   для Німеччини бот окремим рядком покаже, скільки є німецькомовних
   донорів ПОЗА зоною .de.

Про «стеми» й «точні форми». Українські назви відмінюються (Німеччина,
Німеччини, Німеччину), тому зазвичай зберігається незмінний початок слова.
Але для кількох країн так робити не можна: стем «дані» для Данії ловив би
звичайне слово «дані». Для таких випадків перелічені точні форми.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

from app.dictionary.languages import Language, language_by_code
from app.dictionary.normalize import normalize_text


@dataclass(frozen=True, slots=True)
class Country:
    """Одна країна: як її назвати, які в неї зони і яка основна мова."""

    code: str
    """Код країни: "de", "gb". Не плутати з кодом мови — це різні словники."""

    name_uk: str
    """Назва українською: «Німеччина»."""

    name_en: str
    """Назва англійською: «Germany»."""

    zones: tuple[str, ...]
    """Справжні доменні зони країни: (".co.uk", ".uk"). З крапкою."""

    primary_language: str
    """Код основної мови країни. Для Німеччини — "de" (німецька)."""

    flag: str
    """Прапорець для кнопок і карток."""

    region: str
    """Регіон — щоб пропонувати суміжні країни: "europe", "asia"..."""

    stems_uk: tuple[str, ...] = ()
    """Незмінні початки українських назв: «німеччин»."""

    exact_uk: tuple[str, ...] = ()
    """Точні форми — там, де стем був би небезпечний («данія», «данії»)."""

    synonyms: frozenset[str] = field(default_factory=frozenset)
    """Однослівні синоніми: «germany», «deutschland»."""

    phrases: tuple[str, ...] = ()
    """Синоніми з кількох слів: «united kingdom», «південна африка»."""

    @property
    def language(self) -> Language | None:
        """Об'єкт основної мови країни."""
        return language_by_code(self.primary_language)

    @property
    def main_zone(self) -> str:
        """Головна зона — та, яку показуємо в тексті: «.de», «.co.uk»."""
        return self.zones[0] if self.zones else ""

    @property
    def zones_label(self) -> str:
        """Усі зони одним рядком: «.co.uk / .uk»."""
        return " / ".join(self.zones)


# ---------------------------------------------------------------------------
# Таблиця країн.
#
# Порядок полів:
#   код, назва укр, назва англ, зони, основна мова, прапор, регіон,
#   стеми, точні форми, синоніми, фрази
# ---------------------------------------------------------------------------
_RAW: tuple[tuple, ...] = (
    # ---------------------------- Європа ----------------------------------
    ("de", "Німеччина", "Germany", (".de",), "de", "🇩🇪", "europe",
     ("німеччин",), (), ("germany", "deutschland"), ()),
    ("fr", "Франція", "France", (".fr",), "fr", "🇫🇷", "europe",
     ("франці",), (), ("france",), ()),
    ("it", "Італія", "Italy", (".it",), "it", "🇮🇹", "europe",
     ("італі",), (), ("italy", "italia"), ()),
    ("es", "Іспанія", "Spain", (".es",), "es", "🇪🇸", "europe",
     ("іспані",), (), ("spain", "espana"), ()),
    ("nl", "Нідерланди", "Netherlands", (".nl",), "nl", "🇳🇱", "europe",
     ("нідерланд", "голланді"), (), ("netherlands", "holland"), ()),
    ("pl", "Польща", "Poland", (".pl",), "pl", "🇵🇱", "europe",
     ("польщ",), (), ("poland", "polska"), ()),
    ("pt", "Португалія", "Portugal", (".pt",), "pt", "🇵🇹", "europe",
     ("португалі",), (), ("portugal",), ()),
    ("se", "Швеція", "Sweden", (".se",), "sv", "🇸🇪", "europe",
     ("швеці",), (), ("sweden", "sverige"), ()),
    ("cz", "Чехія", "Czechia", (".cz",), "cs", "🇨🇿", "europe",
     ("чехі",), (), ("czechia",), ("czech republic",)),
    ("ro", "Румунія", "Romania", (".ro",), "ro", "🇷🇴", "europe",
     ("румуні",), (), ("romania",), ()),
    ("gr", "Греція", "Greece", (".gr",), "el", "🇬🇷", "europe",
     ("греці",), (), ("greece",), ()),
    ("hu", "Угорщина", "Hungary", (".hu",), "hu", "🇭🇺", "europe",
     ("угорщин",), (), ("hungary",), ()),
    ("gb", "Британія", "United Kingdom", (".co.uk", ".uk"), "en", "🇬🇧", "europe",
     ("британі", "англі"), (), ("britain", "england", "uk"),
     ("great britain", "united kingdom", "велика британія")),
    ("at", "Австрія", "Austria", (".at",), "de", "🇦🇹", "europe",
     ("австрі",), (), ("austria",), ()),
    ("ch", "Швейцарія", "Switzerland", (".ch",), "de", "🇨🇭", "europe",
     ("швейцарі",), (), ("switzerland",), ()),
    ("be", "Бельгія", "Belgium", (".be",), "nl", "🇧🇪", "europe",
     ("бельгі",), (), ("belgium",), ()),
    ("dk", "Данія", "Denmark", (".dk",), "da", "🇩🇰", "europe",
     (), ("данія", "данії", "данію", "данією"), ("denmark",), ()),
    ("no", "Норвегія", "Norway", (".no",), "no", "🇳🇴", "europe",
     ("норвегі",), (), ("norway",), ()),
    ("fi", "Фінляндія", "Finland", (".fi",), "fi", "🇫🇮", "europe",
     ("фінлянді",), (), ("finland",), ()),
    ("ie", "Ірландія", "Ireland", (".ie",), "en", "🇮🇪", "europe",
     ("ірланді",), (), ("ireland",), ()),
    ("bg", "Болгарія", "Bulgaria", (".bg",), "bg", "🇧🇬", "europe",
     ("болгарі",), (), ("bulgaria",), ()),
    ("hr", "Хорватія", "Croatia", (".hr",), "hr", "🇭🇷", "europe",
     ("хорваті",), (), ("croatia",), ()),
    ("rs", "Сербія", "Serbia", (".rs",), "sr", "🇷🇸", "europe",
     ("сербі",), (), ("serbia",), ()),
    ("sk", "Словаччина", "Slovakia", (".sk",), "sk", "🇸🇰", "europe",
     ("словаччин",), (), ("slovakia",), ()),
    ("si", "Словенія", "Slovenia", (".si",), "sl", "🇸🇮", "europe",
     ("словені",), (), ("slovenia",), ()),
    ("lt", "Литва", "Lithuania", (".lt",), "lt", "🇱🇹", "europe",
     ("литв",), (), ("lithuania",), ()),
    ("lv", "Латвія", "Latvia", (".lv",), "lv", "🇱🇻", "europe",
     ("латві",), (), ("latvia",), ()),
    ("ee", "Естонія", "Estonia", (".ee",), "et", "🇪🇪", "europe",
     ("естоні",), (), ("estonia",), ()),
    ("ua", "Україна", "Ukraine", (".ua", ".com.ua"), "uk", "🇺🇦", "europe",
     ("україн",), (), ("ukraine",), ()),
    # ------------------------ Північна Америка ----------------------------
    ("us", "США", "United States", (".us",), "en", "🇺🇸", "north_america",
     ("америк",), ("сша", "usa"), (), ("united states", "сполучені штати")),
    ("ca", "Канада", "Canada", (".ca",), "en", "🇨🇦", "north_america",
     ("канад",), (), ("canada",), ()),
    ("mx", "Мексика", "Mexico", (".mx", ".com.mx"), "es", "🇲🇽", "north_america",
     ("мексик",), (), ("mexico",), ()),
    # ------------------------ Південна Америка ----------------------------
    ("br", "Бразилія", "Brazil", (".com.br", ".br"), "pt", "🇧🇷", "south_america",
     ("бразилі",), (), ("brazil", "brasil"), ()),
    ("ar", "Аргентина", "Argentina", (".com.ar", ".ar"), "es", "🇦🇷", "south_america",
     ("аргентин",), (), ("argentina",), ()),
    ("cl", "Чилі", "Chile", (".cl",), "es", "🇨🇱", "south_america",
     (), ("чилі",), ("chile",), ()),
    ("co", "Колумбія", "Colombia", (".com.co",), "es", "🇨🇴", "south_america",
     ("колумбі",), (), ("colombia",), ()),
    ("pe", "Перу", "Peru", (".com.pe", ".pe"), "es", "🇵🇪", "south_america",
     (), ("перу",), ("peru",), ()),
    # ------------------------------ Азія ----------------------------------
    ("jp", "Японія", "Japan", (".jp",), "ja", "🇯🇵", "asia",
     ("японі",), (), ("japan",), ()),
    ("in", "Індія", "India", (".in", ".co.in"), "hi", "🇮🇳", "asia",
     ("інді",), (), ("india",), ()),
    ("tr", "Туреччина", "Turkey", (".com.tr", ".tr"), "tr", "🇹🇷", "asia",
     ("туреччин",), (), ("turkey", "turkiye"), ()),
    ("vn", "Вʼєтнам", "Vietnam", (".vn", ".com.vn"), "vi", "🇻🇳", "asia",
     ("вєтнам",), (), ("vietnam",), ()),
    ("id", "Індонезія", "Indonesia", (".id", ".co.id"), "id", "🇮🇩", "asia",
     ("індонезі",), (), ("indonesia",), ()),
    ("pk", "Пакистан", "Pakistan", (".pk", ".com.pk"), "ur", "🇵🇰", "asia",
     ("пакистан",), (), ("pakistan",), ()),
    ("ir", "Іран", "Iran", (".ir",), "fa", "🇮🇷", "asia",
     ("іран",), (), ("iran",), ()),
    ("il", "Ізраїль", "Israel", (".co.il", ".il"), "he", "🇮🇱", "asia",
     ("ізраїл",), (), ("israel",), ()),
    ("ae", "ОАЕ", "United Arab Emirates", (".ae",), "ar", "🇦🇪", "asia",
     ("емірат",), ("оае", "uae"), (), ("united arab emirates",)),
    ("sa", "Саудівська Аравія", "Saudi Arabia", (".com.sa", ".sa"), "ar", "🇸🇦", "asia",
     ("саудів",), (), (), ("saudi arabia",)),
    ("th", "Таїланд", "Thailand", (".co.th", ".th"), "th", "🇹🇭", "asia",
     ("таїланд", "таіланд"), (), ("thailand",), ()),
    ("my", "Малайзія", "Malaysia", (".com.my", ".my"), "ms", "🇲🇾", "asia",
     ("малайзі",), (), ("malaysia",), ()),
    ("kr", "Корея", "South Korea", (".co.kr", ".kr"), "ko", "🇰🇷", "asia",
     (), ("корея", "кореї", "корею", "кореєю"), (), ("south korea", "південна корея")),
    ("cn", "Китай", "China", (".cn", ".com.cn"), "zh", "🇨🇳", "asia",
     (), ("китай", "китаю", "китаєм", "китаї"), ("china",), ()),
    ("ph", "Філіппіни", "Philippines", (".ph", ".com.ph"), "tl", "🇵🇭", "asia",
     ("філіппін", "філіпін"), (), ("philippines",), ()),
    # ----------------------------- Африка ---------------------------------
    ("za", "ПАР", "South Africa", (".co.za",), "en", "🇿🇦", "africa",
     (), ("пар",), (), ("south africa", "південна африка", "південно-африканська")),
    ("ng", "Нігерія", "Nigeria", (".com.ng", ".ng"), "en", "🇳🇬", "africa",
     ("нігері",), (), ("nigeria",), ()),
    ("eg", "Єгипет", "Egypt", (".com.eg", ".eg"), "ar", "🇪🇬", "africa",
     ("єгипт", "єгипет"), (), ("egypt",), ()),
    # ---------------------------- Океанія ---------------------------------
    ("au", "Австралія", "Australia", (".com.au", ".au"), "en", "🇦🇺", "oceania",
     ("австралі",), (), ("australia",), ()),
    ("nz", "Нова Зеландія", "New Zealand", (".co.nz", ".nz"), "en", "🇳🇿", "oceania",
     ("зеланді",), (), (), ("new zealand", "нова зеландія")),
)  # fmt: skip


# Двобуквені синоніми, яким МОЖНА довіряти у вільному тексті.
#
# Загальне правило — голі двобуквені коди не використовуються: «in», «is»,
# «no», «it» це звичайні слова, і вони давали б хибні збіги. Але «uk» ні з
# чим не збігається, а в SEO так пишуть постійно, тому це виняток.
_SAFE_SHORT_SYNONYMS = frozenset({"uk"})


def _build() -> dict[str, Country]:
    """Складає таблицю вище в готові об'єкти Country."""
    countries: dict[str, Country] = {}

    for (
        code, name_uk, name_en, zones, language, flag, region,
        stems, exact, synonyms, phrases,
    ) in _RAW:  # fmt: skip
        # Англійська назва теж працює як синонім — але тільки якщо вона з
        # одного слова. Багатослівні («United Kingdom») ідуть у phrases.
        all_synonyms = set(synonyms)
        if " " not in name_en:
            all_synonyms.add(name_en)
        # Двобуквені синоніми відкидаємо (крім явних винятків вище): "in",
        # "no", "it" збігаються зі звичайними словами і дають хибні збіги.
        # Зона з крапкою (".in") — інша річ, вона однозначна й дозволена.
        normalized_synonyms = {normalize_text(s) for s in all_synonyms}
        normalized_synonyms = {
            s for s in normalized_synonyms if len(s) > 2 or s in _SAFE_SHORT_SYNONYMS
        }

        countries[code] = Country(
            code=code,
            name_uk=name_uk,
            name_en=name_en,
            zones=tuple(zones),
            primary_language=language,
            flag=flag,
            region=region,
            stems_uk=tuple(normalize_text(s) for s in stems),
            exact_uk=tuple(normalize_text(s) for s in exact),
            synonyms=frozenset(normalized_synonyms),
            phrases=tuple(normalize_text(p) for p in phrases),
        )

    return countries


COUNTRIES: dict[str, Country] = MappingProxyType(_build())
"""Усі країни: код → Country."""

# Зона → країна. Будується автоматично, тому розбіжність неможлива.
_BY_ZONE: dict[str, Country] = MappingProxyType(
    {zone: country for country in COUNTRIES.values() for zone in country.zones}
)

ALL_COUNTRY_ZONES: frozenset[str] = frozenset(_BY_ZONE)
"""Усі зони, закріплені за країнами."""


def country_by_code(code: str) -> Country | None:
    """Країна за кодом: "de" → Німеччина."""
    return COUNTRIES.get(code)


def country_by_zone(zone: str) -> Country | None:
    """Країна за доменною зоною: ".co.uk" → Британія.

    Для глобальних зон (.com, .net, .online) повертає None — і це правильно:
    вони не належать жодній країні.
    """
    return _BY_ZONE.get(zone.lower())


def countries_with_language(language_code: str, exclude: str | None = None) -> tuple[Country, ...]:
    """Країни, де ця мова основна.

    Саме звідси беруться рекомендації «суміжні країни зі спільною мовою»:
    для Німеччини це Австрія (.at) і Швейцарія (.ch).
    """
    return tuple(
        country
        for country in COUNTRIES.values()
        if country.primary_language == language_code and country.code != exclude
    )


def countries_in_region(region: str, exclude: str | None = None) -> tuple[Country, ...]:
    """Країни того самого регіону — для пропозиції суміжних гео."""
    return tuple(
        country
        for country in COUNTRIES.values()
        if country.region == region and country.code != exclude
    )
