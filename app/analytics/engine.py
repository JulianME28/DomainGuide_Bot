"""Виконання запиту: фільтри, підрахунки й модель країни.

═══════════════════════════════════════════════════════════════════════════
МОДЕЛЬ КРАЇНИ — водоспад із трьох кроків
═══════════════════════════════════════════════════════════════════════════

У даних немає готової колонки «країна». Є ТРИ незалежні сигнали, і на запит
про країну донор зараховується за ПЕРШИМ, що спрацював (водоспад — без
подвійного рахунку, кожен наступний крок бере лише ще не порахованих):

  (а) ЗОНА     — доменна зона ∈ ccTLD країни (Франція → .fr).
  (в) GEO      — код країни в колонці GEO і трафік N > 0. Зона тут не важлива:
                 донор із зоною .de і GEO «(fr, 5000)» на запит про Францію
                 рахується сюди, а на запит про Німеччину — у крок (а).
  (б) МОВА     — основна мова країни І зона ∈ GLOBAL_ZONES (нейтральні:
                 .com .net .org …). Зони ІНШИХ країн (.de, .be) сюди не входять.

СПІЛЬНІ МОВИ (en, es, pt, ar — ті, у кого language.widespread) крок (б) у
підсумок НЕ додають. Причина з реальної роботи: англійської в базі ~17 000,
і якби кожна англомовна країна забирала одні й ті самі .com-сайти, Британія
й Ірландія показували б по 14 000 «своїх» донорів — неправда. Тому:

    однозначна мова (fr, de, it, …):  підсумок = зона + GEO + мова-на-GLOBAL
    спільна мова    (en, es, pt, ar): підсумок = зона + GEO

Один запит — кожен донор рівно в одній групі. Похибка й «ядро + запас»
рахуються від ПІДСУМКУ, а не від окремої складової.

ОКРЕМІ РЯДКИ-ПРОПОЗИЦІЇ (у підсумок НЕ входять, без подвійного показу):

    💬 французькою на зонах інших країн — N     ← мова на ccTLD інших країн
    💬 англійською на нейтральних зонах — N     ← лише для спільних мов

Другий рядок — це якраз крок (б), винесений з підсумку для спільних мов.

GEO є лише в «Меджику». «Морди» колонки GEO не мають — там крок (в) просто
відсутній (0), підрахунок працює на двох кроках, нічого не обнуляється.

═══════════════════════════════════════════════════════════════════════════
БЕЗПЕКА
═══════════════════════════════════════════════════════════════════════════

Усе, що виходить із цього модуля, — це числа. QueryResult не містить
списку донорів. Домени не можуть витекти у відповідь навіть помилково,
бо шар відображення їх просто не отримує.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app.analytics.query import Dimension, DonorQuery, QueryKind
from app.data.models import Dataset, Donor
from app.dictionary.countries import Country, country_by_zone
from app.dictionary.languages import Language, display_language
from app.dictionary.zones import is_global_zone

# Допустима похибка з ТЗ: нижня межа = знайдена кількість × 0.7.
ERROR_MARGIN = 0.3

# Якщо середнє порахували менш ніж на трьох донорах — воно ненадійне.
MIN_RELIABLE_SAMPLE = 3

# Якщо середній DR або трафік нижчий за це — група слабка (ТЗ, розділ 7.6).
WEAK_METRIC_THRESHOLD = 3

# Скільки країн максимум за один запит-список. Більше — рахувати відмовляємось
# (це вже не осмислений запит, а спроба перебрати півсвіту одним рядком).
MAX_MULTI_COUNTRIES = 30

# Групи розподілу за АБСОЛЮТНОЮ кількістю заспамлених лінків.
# Кожен запис — (підпис, нижня межа, верхня межа|None). Порядок сталий.
SPAM_GROUPS: tuple[tuple[str, int, int | None], ...] = (
    ("0", 0, 0),
    ("1-20", 1, 20),
    ("21-50", 21, 50),
    ("51-100", 51, 100),
    ("100+", 101, None),
)


@dataclass(frozen=True, slots=True)
class Aggregate:
    """Підрахунки по групі донорів. Списку донорів тут немає — навмисно."""

    count: int
    avg_dr: float | None = None
    avg_traffic: float | None = None
    dr_sample: int = 0
    """На скількох донорах порахований середній DR (у решти стояло «n/a»)."""

    traffic_sample: int = 0

    # Скільки донорів мають РІВНО 0 (не порожньо!) у метриці. Нуль входить у
    # середнє й тягне його вниз, а на око його не видно — тому рахуємо окремо.
    dr_zeros: int = 0
    traffic_zeros: int = 0
    outlinks_zeros: int = 0

    # Аналіз заспамленості («Морди»). Для «Меджика» лишаються None/().
    avg_outlinks: float | None = None
    outlinks_sample: int = 0

    spam_distribution: tuple[tuple[str, int], ...] = ()
    """Розподіл донорів за АБСОЛЮТНОЮ кількістю заспамлених лінків, групами
    «0 / 1-20 / 21-50 / 51-100 / 100+». Групи з нулем донорів не включені.
    Донори з порожнім значенням спаму сюди не потрапляють."""

    @property
    def min_estimate(self) -> int:
        """Нижня межа з урахуванням похибки 30%: кількість × 0.7.

        Рахуємо цілими числами навмисно. Комп'ютер зберігає 0.7 неточно, і
        85 × 0.7 у нього виходить 59.4999... — округлення дало б 59 замість
        правильних 60. Формула (n × 7 + 5) // 10 дає той самий результат,
        але без цієї пастки.
        """
        return (self.count * 7 + 5) // 10

    @property
    def low_sample(self) -> bool:
        """Чи середні порахували на надто малій кількості донорів.

        Враховуються лише ті показники, які реально є: для «Меджика»
        outlinks_sample дорівнює 0, тож у перевірку не потрапляє, і
        поведінка «Меджика» не змінюється.
        """
        if self.count == 0:
            return False
        samples = [s for s in (self.dr_sample, self.traffic_sample, self.outlinks_sample) if s > 0]
        if not samples:
            return True
        return min(samples) < MIN_RELIABLE_SAMPLE

    @property
    def weak_metrics(self) -> bool:
        """Чи показники групи низькі (ТЗ, розділ 7.6)."""
        values = [v for v in (self.avg_dr, self.avg_traffic) if v is not None]
        return bool(values) and min(values) < WEAK_METRIC_THRESHOLD

    @property
    def is_empty(self) -> bool:
        return self.count == 0


@dataclass(frozen=True, slots=True)
class CountrySplit:
    """Розклад підсумку країни на три складові водоспаду.

    total = zone + language + geo. Показується в рядку «Знайдено донорів»:
    «562 (.fr 159 | мова 331 | GEO 72)».
    """

    zone: int
    language: int
    geo: int
    main_zone: str
    """Головна ccTLD країни для підпису зонової складової: «.fr»."""

    show_geo: bool
    """Чи показувати GEO-складову. False для баз без колонки GEO («Морди»)."""

    show_language: bool = True
    """Чи показувати складову «мова» і чи входить вона в підсумок.

    False для спільних мов (en, es, pt, ar): там мова-на-нейтральних-зонах
    у підсумок не входить, а виноситься окремим рядком-пропозицією."""

    @property
    def total(self) -> int:
        base = self.zone + self.geo
        return base + self.language if self.show_language else base


@dataclass(frozen=True, slots=True)
class LanguageAddendum:
    """Останній рядок картки — окрема пропозиція, у підсумок НЕ входить.

    Читається так: «мовою країни на ccTLD інших країн є ще N донорів, яких
    немає в підсумку». Це не «поза зоною» загалом, а саме чужі ccTLD:
    нейтральні (.com) уже враховані в кроці (б) підсумку, а зона країни — у (а).
    """

    language: Language
    count: int
    zone_label: str
    """Зона (чи зони) країни: «.de», «.co.uk / .uk». Для контексту."""

    country_name: str = ""
    """Назва країни — потрібна для тексту попередження про спільні мови."""

    @property
    def needs_warning(self) -> bool:
        """Чи попереджати, що цією мовою пишуть багато країн."""
        return self.language.widespread


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Готовий результат запиту. Тільки числа."""

    section_title: str
    query: DonorQuery
    core: Aggregate
    split: CountrySplit | None = None
    """Розклад підсумку на зону/мову/GEO — лише для запиту про країну."""

    addendum: LanguageAddendum | None = None
    """Рядок «мовою на зонах інших країн» — донори на ccTLD інших країн."""

    neutral_offer: LanguageAddendum | None = None
    """Рядок «мовою на нейтральних зонах» — лише для спільних мов: крок (б),
    винесений із підсумку (щоб Британія не забирала всі .com-сайти)."""

    zone_breakdown: tuple[tuple[str, int], ...] = ()
    language_breakdown: tuple[tuple[str, int], ...] = ()
    country_breakdown: tuple[tuple[str, int], ...] = ()
    available: bool = True
    error: str | None = None
    total_in_base: int = 0
    """Скільки всього донорів у базі — щоб було з чим порівняти."""

    tracks_spam: bool = False
    """Чи показувати в картці вихідні лінки й заспамленість (лише «Морди»)."""

    dropped_dimensions: frozenset[str] = frozenset()
    """Виміри, фільтр по яких запит мав, а база не має відповідних колонок —
    тому фільтр мовчки НЕ застосувався. Картка попереджає про це, щоб число не
    вводило в оману (напр. фільтр заспамленості в «Меджику»)."""

    stale: bool = False
    """True, якщо числа з кешу (онлайн-оновлення щойно не вдалося)."""

    as_of: float | None = None
    """Час останнього успішного оновлення (для помітки про застарілість)."""


@dataclass(frozen=True, slots=True)
class MultiCountryResult:
    """Результат запиту по СПИСКУ країн. Тільки числа, донорів тут немає.

    Розподіл ЕКСКЛЮЗИВНИЙ: кожен донор належить рівно ОДНІЙ країні зі списку —
    тій, чия претензія найсильніша за водоспадом (зона > GEO > мова), а при
    рівному пріоритеті — країні, що стоїть раніше в запиті. Тому сума кількостей
    по країнах дорівнює загальній кількості унікальних донорів (`unique.count`),
    і жоден донор не рахується двічі.
    """

    section_title: str
    query: DonorQuery
    per_country: tuple[tuple[Country, CountrySplit], ...]
    """(країна, розклад складових) — відсортовано за спаданням кількості.

    CountrySplit несе зону/мову/GEO цієї країни в ЕКСКЛЮЗИВНОМУ розподілі."""

    unique: Aggregate
    """Підрахунки по всьому набору донорів (кожен рівно раз)."""

    unrecognized: tuple[str, ...] = ()
    """Назви зі списку, які не вдалося впізнати як країну."""

    available: bool = True
    error: str | None = None
    tracks_spam: bool = False
    stale: bool = False
    as_of: float | None = None


# ---------------------------------------------------------------------------
# Фільтрація
# ---------------------------------------------------------------------------


def _in_range(value: float | None, minimum: float | None, maximum: float | None) -> bool:
    """Чи значення потрапляє в діапазон.

    Донор без значення (в таблиці стояло «n/a») проходить фільтр лише тоді,
    коли фільтра немає. Інакше вийшло б, що «DR від 30» пропускає донорів,
    у яких DR узагалі невідомий.
    """
    if minimum is None and maximum is None:
        return True
    if value is None:
        return False
    if minimum is not None and value < minimum:
        return False
    return not (maximum is not None and value > maximum)


def passes_metrics(donor: Donor, query: DonorQuery) -> bool:
    """Чи проходить донор усі числові фільтри: DR, трафік, вихідні, спам.

    Заспамленість фільтрується по АБСОЛЮТНІЙ КІЛЬКОСТІ заспамлених лінків
    (donor.spammed), а не по відсотку. «Заспамленість до 40» = до 40
    заспамлених лінків. Донор із порожнім значенням спаму (spammed = None)
    при заданому фільтрі не проходить — так само, як донор без DR не
    проходить фільтр по DR.
    """
    if query.geo is not None and not (donor.geo_code == query.geo.code and donor.has_measured_geo):
        # Фільтр по колонці GEO: країна походження трафіку з N>0. GEO=0 або
        # інша країна — не проходить, незалежно від доменної зони й мови.
        return False
    if not (
        _in_range(donor.dr, query.dr_min, query.dr_max)
        and _in_range(donor.traffic, query.traffic_min, query.traffic_max)
        and _in_range(donor.spammed, query.spam_min, query.spam_max)
    ):
        return False
    # СЛУЖБОВА роль стовпця F («вихідні»): коли заданий БУДЬ-ЯКИЙ фільтр
    # заспамленості (G), рядок проходить лише якщо вихідних > 0. Мертвий сайт
    # (F=0) — не «чистий», а непрацюючий (дані не оновились), тож у якісний запит
    # не входить (як і в розподілі, де група «0,0» — найгірша). Саме числом F
    # НЕ фільтрується — тільки цей відсів нуля.
    if query.spam_min is not None or query.spam_max is not None:
        return donor.outlinks is not None and donor.outlinks > 0
    return True


def passes_core(donor: Donor, query: DonorQuery) -> bool:
    """Чи потрапляє донор у ядро запиту (для НЕ-країнних запитів).

    Це проста перевірка «зона + мова + метрики». Запит про КРАЇНУ рахується
    інакше — моделлю країни (див. _country_bucket / classify_country).
    """
    zones = query.core_zones
    if zones and donor.zone not in zones:
        return False

    languages = query.core_languages
    if languages and donor.language not in languages:
        return False

    return passes_metrics(donor, query)


def select_core(dataset: Dataset, query: DonorQuery) -> list[Donor]:
    """Донори, які потрапляють у ядро запиту.

    Функція внутрішня: назовні з модуля йдуть тільки числа.
    """
    return [donor for donor in dataset.donors if passes_core(donor, query)]


# ---------------------------------------------------------------------------
# Модель країни: трикроковий водоспад
# ---------------------------------------------------------------------------


def _is_widespread(country) -> bool:
    """Чи основна мова країни спільна (en, es, pt, ar) — нею пишуть багато країн."""
    return bool(country and country.language and country.language.widespread)


def _country_bucket(donor: Donor, country) -> str | None:
    """До якого букета країни належить донор:

        "zone"        — доменна зона країни (в підсумку);
        "geo"         — GEO-код країни з N>0 (в підсумку);
        "lang_global" — мова країни на нейтральній зоні (.com/.net);
        "lang_other"  — мова країни на ccTLD ІНШОЇ країни;
        None          — жоден.

    Пріоритет: зона → GEO → мова. Саме тому .com-донор із GEO країни
    потрапляє в «geo» (підсумок), а не в «lang_global» — без подвійного
    рахунку між пропозицією й підсумком. Метрики тут НЕ перевіряються.

    Чи входить букет у підсумок, вирішує вже _in_country_total: для спільних
    мов «lang_global» у підсумок не йде.
    """
    if donor.zone in country.zones:
        return "zone"
    if donor.geo_code == country.code and donor.has_measured_geo:
        return "geo"
    keys = country.language.data_keys if country.language else frozenset()
    if donor.language in keys:
        if is_global_zone(donor.zone):
            return "lang_global"
        other = country_by_zone(donor.zone)
        if other is not None and other.code != country.code:
            return "lang_other"
    return None


def classify_country(
    dataset: Dataset, query: DonorQuery
) -> tuple[list[Donor], list[Donor], list[Donor], list[Donor]]:
    """Ділить донорів на групи для запиту про країну — за ОДИН прохід.

    Повертає (зона, мова-на-нейтральних, geo, мова-на-інших-зонах). Кожен
    донор рівно в одному букеті. Усі відфільтровані метриками запиту.

    Що входить у ПІДСУМОК, залежить від мови (див. run_query): зона й geo —
    завжди; мова-на-нейтральних — лише для однозначних мов. Мова-на-інших —
    ніколи (це окрема пропозиція).
    """
    country = query.country
    zone_d: list[Donor] = []
    lang_global_d: list[Donor] = []
    geo_d: list[Donor] = []
    lang_other_d: list[Donor] = []

    buckets = {
        "zone": zone_d,
        "geo": geo_d,
        "lang_global": lang_global_d,
        "lang_other": lang_other_d,
    }

    for donor in dataset.donors:
        if not passes_metrics(donor, query):
            continue
        bucket = _country_bucket(donor, country)
        if bucket is not None:
            buckets[bucket].append(donor)

    return zone_d, lang_global_d, geo_d, lang_other_d


def _in_country_total(donor: Donor, country) -> bool:
    """Чи входить донор у ПІДСУМОК країни (без перевірки метрик).

    Зона й GEO — завжди. Мова на нейтральних зонах — лише для однозначних
    мов; для спільних (en, es, pt, ar) вона винесена окремим рядком.
    """
    bucket = _country_bucket(donor, country)
    if bucket in ("zone", "geo"):
        return True
    if bucket == "lang_global":
        return not _is_widespread(country)
    return False


# Пріоритет складової водоспаду → назва букета. Менший пріоритет — сильніший.
_CLAIM_BUCKET = {0: "zone", 1: "geo", 2: "language"}


def _country_claim(donor: Donor, country, widespread: bool) -> int | None:
    """Наскільки СИЛЬНО донор претендує на країну — для ексклюзивного розподілу.

    0 — зона (найсильніше), 1 — GEO, 2 — мова на нейтральній зоні (лише для
    однозначних мов). None — не претендує. Це той самий водоспад, лише як
    число-пріоритет: у мультизапиті донор дістається країні з найменшим
    пріоритетом, а при рівності пріоритетів — тій, що раніше в запиті.
    """
    bucket = _country_bucket(donor, country)
    if bucket == "zone":
        return 0
    if bucket == "geo":
        return 1
    if bucket == "lang_global" and not widespread:
        return 2
    return None


def passes_result(donor: Donor, query: DonorQuery) -> bool:
    """Чи входить донор у ПІДСУМОК запиту.

    Для запиту про країну — модель країни; для решти — звичайне ядро.
    Спільний предикат, щоб рекомендації рахували те саме число, що й картка.
    """
    if query.kind is QueryKind.COUNTRY and query.country is not None:
        return passes_metrics(donor, query) and _in_country_total(donor, query.country)
    return passes_core(donor, query)


def result_count(dataset: Dataset, query: DonorQuery) -> int:
    """Скільки донорів у підсумку запиту (з урахуванням моделі країни)."""
    return sum(1 for donor in dataset.donors if passes_result(donor, query))


@dataclass(frozen=True, slots=True)
class CrossBaseTotal:
    """Підсумок по КІЛЬКОХ базах разом — самі числа, без жодного домену.

    Рахувати простою сумою `56 + 279` не можна: один і той самий сайт може бути
    і в «Меджику», і в «Мордах», і тоді його порахували б двічі. Тому підсумок —
    це кількість УНІКАЛЬНИХ доменів (об'єднання множин), а `overlap` — скільки
    доменів опинилось одразу в обох базах (перетин).

    БЕЗПЕКА: домени порівнюються всередині шару аналітики й НАЗОВНІ не виходять —
    структура містить лише числа (див. CLAUDE.md §2.2, §5).
    """

    per_base: tuple[tuple[str, int], ...]  # (назва бази, скільки в ній)
    unique: int  # скільки унікальних доменів разом

    @property
    def overlap(self) -> int:
        """Скільки донорів «зайві» через дублювання = скільки є в обох базах.

        Для двох баз без внутрішніх дублів це рівно перетин |A ∩ B|:
        |A| + |B| − |A ∪ B|.
        """
        return sum(count for _title, count in self.per_base) - self.unique


def cross_base_total(bases: list[tuple[str, Dataset, DonorQuery]]) -> CrossBaseTotal:
    """Підрахунок по кількох базах разом: скільки в кожній і скільки унікальних.

    Один прохід на базу: додаємо домен кожного відповідного донора в спільну
    множину `seen`. Множина сама прибирає повтори, тож вкладені цикли й
    попарні порівняння не потрібні — складність лінійна від кількості рядків.

    Домени живуть лише в цій функції: назовні повертаємо самі числа.
    """
    seen: set[str] = set()
    per_base: list[tuple[str, int]] = []
    for title, dataset, query in bases:
        normalized = normalize_query(dataset, query)
        count = 0
        for donor in dataset.donors:
            if passes_result(donor, normalized):
                count += 1
                seen.add(donor.domain)
        per_base.append((title, count))
    return CrossBaseTotal(per_base=tuple(per_base), unique=len(seen))


def _build_offer(country, donors: list[Donor]) -> LanguageAddendum | None:
    """Складає рядок-пропозицію (на зонах інших країн / на нейтральних зонах)."""
    language = country.language if country else None
    if language is None or not donors:
        return None
    return LanguageAddendum(
        language=language,
        count=len(donors),
        zone_label=country.zones_label,
        country_name=country.name_uk,
    )


# ---------------------------------------------------------------------------
# Підрахунки
# ---------------------------------------------------------------------------


def spam_distribution(donors: list[Donor]) -> tuple[tuple[str, int], ...]:
    """Розподіл донорів за кількістю ЗАСПАМЛЕНИХ лінків, групами.

    Правило «0,0» (навмисне, не помилка): якщо і вихідних лінків 0, і
    заспамлених 0 — це непрацюючий сайт, дані по якому просто не оновились.
    Такого донора зараховуємо в найгіршу групу «100+».

    Донори з порожнім значенням спаму (spammed = None) у групи не потрапляють
    і підрахунок не ламають. Групи з нулем донорів у результат не входять.
    """
    counts = {label: 0 for label, _low, _high in SPAM_GROUPS}

    for donor in donors:
        spammed = donor.spammed
        if spammed is None:
            continue  # порожнє значення — не в групи
        if donor.outlinks == 0 and spammed == 0:
            counts["100+"] += 1  # правило «0,0» — непрацюючий сайт
            continue
        for label, low, high in SPAM_GROUPS:
            if spammed >= low and (high is None or spammed <= high):
                counts[label] += 1
                break

    return tuple((label, counts[label]) for label, _low, _high in SPAM_GROUPS if counts[label])


def aggregate(donors: list[Donor]) -> Aggregate:
    """Рахує кількість, середні й розподіли по групі донорів.

    Донори з «n/a» рахуються в кількості, але в середні не входять — інакше
    середній DR був би заниженим через нулі, яких насправді немає. Окремо
    рахуємо, скільки донорів мають РІВНО 0: нуль у середнє входить і тягне
    його вниз, тому його варто показати поруч.
    """
    if not donors:
        return Aggregate(count=0)

    dr_values = [d.dr for d in donors if d.dr is not None]
    traffic_values = [d.traffic for d in donors if d.traffic is not None]
    outlinks_values = [d.outlinks for d in donors if d.outlinks is not None]

    def average(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    def zeros(values: list[float]) -> int:
        return sum(1 for v in values if v == 0)

    return Aggregate(
        count=len(donors),
        avg_dr=average(dr_values),
        avg_traffic=average(traffic_values),
        dr_sample=len(dr_values),
        traffic_sample=len(traffic_values),
        dr_zeros=zeros(dr_values),
        traffic_zeros=zeros(traffic_values),
        outlinks_zeros=zeros(outlinks_values),
        avg_outlinks=average(outlinks_values),
        outlinks_sample=len(outlinks_values),
        spam_distribution=spam_distribution(donors),
    )


# ---------------------------------------------------------------------------
# Розподіли (для кнопок «уточнити гео» і «розподіл по мовах»)
# ---------------------------------------------------------------------------


def zone_breakdown(donors: list[Donor], limit: int = 8) -> tuple[tuple[str, int], ...]:
    """Розподіл групи по доменних зонах."""
    counter = Counter(donor.zone for donor in donors if donor.zone)
    return tuple(counter.most_common(limit))


def language_breakdown(donors: list[Donor], limit: int = 8) -> tuple[tuple[str, int], ...]:
    """Розподіл групи по мовах — уже з гарними українськими назвами."""
    counter = Counter(donor.language for donor in donors if donor.language)
    return tuple((display_language(value), count) for value, count in counter.most_common(limit))


def country_breakdown(donors: list[Donor], limit: int = 8) -> tuple[tuple[str, int], ...]:
    """Розподіл групи по країнах — виводиться з доменних зон.

    Глобальні зони (.com, .net) зводяться в окремий рядок і НЕ приписуються
    жодній країні. Це принципово: сайт на .com може бути звідки завгодно.
    """
    counter: Counter[str] = Counter()
    global_count = 0
    unknown_count = 0

    for donor in donors:
        if not donor.zone:
            unknown_count += 1
            continue
        if is_global_zone(donor.zone):
            global_count += 1
            continue
        country = country_by_zone(donor.zone)
        if country is None:
            # Зона є, але вона не наша й не глобальна — теж нікому не приписуємо.
            unknown_count += 1
            continue
        counter[f"{country.flag} {country.name_uk}"] += 1

    rows = list(counter.most_common(limit))
    if global_count:
        rows.append(("🌐 Глобальні зони (без країни)", global_count))
    if unknown_count:
        rows.append(("❔ Зона не визначена", unknown_count))
    return tuple(rows)


# ---------------------------------------------------------------------------
# Головна функція
# ---------------------------------------------------------------------------


def normalize_query(dataset: Dataset, query: DonorQuery) -> DonorQuery:
    """Прибирає з запиту виміри, яких база не має.

    Навіщо. Вихідні лінки й заспамленість є лише в «Мордах». Якщо такий
    фільтр якось потрапить у запит до «Меджика» (через вільний текст або
    успадкування зі спаму), кожен донор «Меджика» має ці поля порожніми —
    і фільтр по порожньому відсіяв би геть усіх, давши хибний нуль.

    Тому для баз без заспамленості ці виміри просто ігноруються: у «Меджику»
    аналіз заспамленості не має сенсу й у картці не з'являється.
    """
    # GEO-фільтр має сенс лише там, де є колонка GEO. Для баз без неї знімаємо
    # його, інакше фільтр по порожньому GEO відсіяв би всіх (хибний нуль).
    if not dataset.tracks_geo:
        query = query.without(Dimension.GEO)
    if dataset.tracks_spam:
        return query
    return query.without(Dimension.SPAM)


def unsupported_dimensions(dataset: Dataset, query: DonorQuery) -> frozenset[str]:
    """Виміри, які запит фільтрує, а база не має для них колонок.

    Саме ці фільтри normalize_query мовчки прибирає. Повертаємо їх, щоб картка
    могла чесно попередити: «фільтр не застосовано, бо в цій базі таких даних
    немає». Логіка ДЗЕРКАЛЬНА до normalize_query, щоб перелік точно збігався з
    тим, що справді відкинуто.
    """
    filled = query.filled_dimensions
    dropped: set[str] = set()
    if not dataset.tracks_geo and Dimension.GEO in filled:
        dropped.add(Dimension.GEO)
    if not dataset.tracks_spam and Dimension.SPAM in filled:
        dropped.add(Dimension.SPAM)
    return frozenset(dropped)


def run_query(dataset: Dataset, query: DonorQuery, *, with_breakdowns: bool = True) -> QueryResult:
    """Виконує запит і повертає готові числа.

    Якщо база недоступна — повертається результат із нулями й поясненням.
    Виняток звідси не летить: бот має відповісти, а не впасти.
    """
    if not dataset.available:
        return QueryResult(
            section_title=dataset.title,
            query=query,
            core=Aggregate(count=0),
            available=False,
            error=dataset.error,
            tracks_spam=dataset.tracks_spam,
        )

    # Рахуємо, які фільтри база не потягне, ДО того як normalize_query їх зніме.
    dropped = unsupported_dimensions(dataset, query)
    query = normalize_query(dataset, query)

    # Запит про КРАЇНУ рахується моделлю країни; решта — звичайним ядром.
    # Розклад складових і рядки-пропозиції — тільки для країни.
    split: CountrySplit | None = None
    addendum: LanguageAddendum | None = None
    neutral_offer: LanguageAddendum | None = None
    if query.kind is QueryKind.COUNTRY and query.country is not None:
        country = query.country
        zone_d, lang_global_d, geo_d, lang_other_d = classify_country(dataset, query)
        widespread = _is_widespread(country)

        # Мова-на-нейтральних входить у підсумок лише для однозначних мов.
        core_donors = zone_d + geo_d + ([] if widespread else lang_global_d)
        split = CountrySplit(
            zone=len(zone_d),
            language=len(lang_global_d),
            geo=len(geo_d),
            main_zone=country.main_zone,
            show_geo=dataset.tracks_geo,
            show_language=not widespread,
        )
        addendum = _build_offer(country, lang_other_d)
        # Для спільних мов крок (б) виноситься окремим рядком.
        if widespread:
            neutral_offer = _build_offer(country, lang_global_d)
    else:
        core_donors = select_core(dataset, query)

    return QueryResult(
        section_title=dataset.title,
        query=query,
        core=aggregate(core_donors),
        split=split,
        addendum=addendum,
        neutral_offer=neutral_offer,
        zone_breakdown=zone_breakdown(core_donors) if with_breakdowns else (),
        language_breakdown=language_breakdown(core_donors) if with_breakdowns else (),
        country_breakdown=country_breakdown(core_donors) if with_breakdowns else (),
        total_in_base=dataset.count,
        tracks_spam=dataset.tracks_spam,
        dropped_dimensions=dropped,
        stale=dataset.stale,
        as_of=dataset.loaded_at if dataset.stale else None,
    )


def run_multi_country(
    dataset: Dataset,
    query: DonorQuery,
    *,
    unrecognized: tuple[str, ...] = (),
) -> MultiCountryResult:
    """Запит по СПИСКУ країн — за ОДИН прохід по базі.

    Продуктивність (ТЗ, розділ про 31 000 рядків × до 30 країн): зовнішній
    цикл по донорах ОДИН раз, внутрішній — короткий, по країнах списку. Це та
    сама ідея, що у _country_totals рекомендацій (правка 747dfb6), а не окремий
    прохід на кожну країну.

    Розподіл ЕКСКЛЮЗИВНИЙ: кожен донор дістається одній країні за пріоритетом
    водоспаду (зона > GEO > мова), при рівному — країні, раніше в запиті. Тому
    сума по країнах = кількість унікальних донорів, без подвійного рахунку.
    """
    if not dataset.available:
        return MultiCountryResult(
            section_title=dataset.title,
            query=query,
            per_country=(),
            unique=Aggregate(count=0),
            unrecognized=unrecognized,
            available=False,
            error=dataset.error,
            tracks_spam=dataset.tracks_spam,
        )

    query = normalize_query(dataset, query)
    # Порядок країн у запиті — це і є правило розв'язання нічиїх.
    specs = [(country, _is_widespread(country)) for country in query.countries]

    # По кожній країні окремо рахуємо складові зона/мова/GEO (в ексклюзиві).
    zone_counts = {country.code: 0 for country, _ in specs}
    language_counts = {country.code: 0 for country, _ in specs}
    geo_counts = {country.code: 0 for country, _ in specs}
    assigned: list[Donor] = []

    for donor in dataset.donors:
        if not passes_metrics(donor, query):
            continue
        best_key: tuple[int, int] | None = None
        best_code: str | None = None
        best_bucket: str | None = None
        for index, (country, widespread) in enumerate(specs):
            claim = _country_claim(donor, country, widespread)
            if claim is None:
                continue
            key = (claim, index)  # (пріоритет складової, позиція в запиті)
            if best_key is None or key < best_key:
                best_key, best_code, best_bucket = key, country.code, _CLAIM_BUCKET[claim]
        if best_code is None:
            continue
        if best_bucket == "zone":
            zone_counts[best_code] += 1
        elif best_bucket == "geo":
            geo_counts[best_code] += 1
        else:
            language_counts[best_code] += 1
        assigned.append(donor)

    per_country = tuple(
        sorted(
            (
                (
                    country,
                    CountrySplit(
                        zone=zone_counts[country.code],
                        language=language_counts[country.code],
                        geo=geo_counts[country.code],
                        main_zone=country.main_zone,
                        show_geo=dataset.tracks_geo,
                        show_language=not widespread,
                    ),
                )
                for country, widespread in specs
            ),
            key=lambda pair: pair[1].total,
            reverse=True,
        )
    )

    return MultiCountryResult(
        section_title=dataset.title,
        query=query,
        per_country=per_country,
        unique=aggregate(assigned),
        unrecognized=unrecognized,
        tracks_spam=dataset.tracks_spam,
        stale=dataset.stale,
        as_of=dataset.loaded_at if dataset.stale else None,
    )
