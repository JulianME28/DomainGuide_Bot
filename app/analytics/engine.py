"""Виконання запиту: фільтри, підрахунки й модель гео.

═══════════════════════════════════════════════════════════════════════════
МОДЕЛЬ ГЕО — найважливіше в проєкті
═══════════════════════════════════════════════════════════════════════════

У даних НЕМАЄ колонки країни. Її взагалі не існує і не буде. Тому країна
визначається з двох незалежних сигналів: доменної зони і мови сайту.

Питання «скільки донорів по Німеччині» насправді має ДВІ різні відповіді:

    6 донорів у зоні .de          ← сайти з німецьким доменом
    8 донорів німецькою мовою     ← сайти німецькою, будь-де у світі

Це різні множини, і вони перетинаються. Тому бот:

  * бере за ЯДРО доменну зону (.de) — це надійніший сигнал країни;
  * окремим ОСТАННІМ рядком показує, скільки німецькомовних донорів є
    ПОЗА зоною .de — і рахує лише тих, кого немає в ядрі;
  * НІКОЛИ не додає ці два числа одне до одного.

Чому не сумувати. У прикладі вище 6 + 8 = 14 було б неправдою: 4 донори
одночасно і в зоні .de, і німецькою мовою — їх порахували б двічі. Тому
мовний додаток дорівнює 4 (8 німецькомовних мінус 4, що вже в зоні .de),
і показується він окремим рядком, а не в сумі.

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
from app.dictionary.countries import country_by_zone
from app.dictionary.languages import Language, display_language
from app.dictionary.zones import is_global_zone

# Допустима похибка з ТЗ: нижня межа = знайдена кількість × 0.7.
ERROR_MARGIN = 0.3

# Якщо середнє порахували менш ніж на трьох донорах — воно ненадійне.
MIN_RELIABLE_SAMPLE = 3

# Якщо середній DR або трафік нижчий за це — група слабка (ТЗ, розділ 7.6).
WEAK_METRIC_THRESHOLD = 3


@dataclass(frozen=True, slots=True)
class Aggregate:
    """Підрахунки по групі донорів. Списку донорів тут немає — навмисно."""

    count: int
    avg_dr: float | None = None
    avg_traffic: float | None = None
    dr_sample: int = 0
    """На скількох донорах порахований середній DR (у решти стояло «n/a»)."""

    traffic_sample: int = 0

    # Аналіз заспамленості («Морди»). Для «Меджика» лишаються None/0.
    avg_outlinks: float | None = None
    avg_spam_percent: float | None = None
    outlinks_sample: int = 0
    spam_sample: int = 0
    """На скількох донорах порахована середня заспамленість. Донори з 0
    вихідних лінків сюди не входять — у них відсоток невизначений."""

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
        outlinks_sample і spam_sample дорівнюють 0, тож у перевірку не
        потрапляють, і поведінка «Меджика» не змінюється.
        """
        if self.count == 0:
            return False
        samples = [
            s
            for s in (self.dr_sample, self.traffic_sample, self.outlinks_sample, self.spam_sample)
            if s > 0
        ]
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
class LanguageAddendum:
    """Мовний додаток — той самий останній рядок картки.

    Читається так: «крім донорів у зоні .de, є ще N донорів німецькою мовою
    в інших зонах». Із ядром не сумується.
    """

    language: Language
    count: int
    zone_label: str
    """Зона (чи зони) ядра: «.de», «.co.uk / .uk»."""

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
    addendum: LanguageAddendum | None = None
    zone_breakdown: tuple[tuple[str, int], ...] = ()
    language_breakdown: tuple[tuple[str, int], ...] = ()
    country_breakdown: tuple[tuple[str, int], ...] = ()
    available: bool = True
    error: str | None = None
    total_in_base: int = 0
    """Скільки всього донорів у базі — щоб було з чим порівняти."""

    tracks_spam: bool = False
    """Чи показувати в картці вихідні лінки й заспамленість (лише «Морди»)."""


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

    Заспамленість фільтрується по ВІДСОТКУ. Донор із 0 вихідних лінків має
    невизначений відсоток (spam_percent = None), тому при заданому фільтрі
    по заспамленості він не проходить — так само, як донор без DR не
    проходить фільтр по DR.
    """
    return (
        _in_range(donor.dr, query.dr_min, query.dr_max)
        and _in_range(donor.traffic, query.traffic_min, query.traffic_max)
        and _in_range(donor.outlinks, query.outlinks_min, query.outlinks_max)
        and _in_range(donor.spam_percent, query.spam_min, query.spam_max)
    )


def passes_core(donor: Donor, query: DonorQuery) -> bool:
    """Чи потрапляє донор у ядро запиту."""
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
# Підрахунки
# ---------------------------------------------------------------------------


def aggregate(donors: list[Donor]) -> Aggregate:
    """Рахує кількість і середні по групі донорів.

    Донори з «n/a» рахуються в кількості, але в середні не входять — інакше
    середній DR був би заниженим через нулі, яких насправді немає.
    """
    if not donors:
        return Aggregate(count=0)

    dr_values = [d.dr for d in donors if d.dr is not None]
    traffic_values = [d.traffic for d in donors if d.traffic is not None]
    outlinks_values = [d.outlinks for d in donors if d.outlinks is not None]
    # Заспамленість — тільки там, де відсоток визначений (вихідних > 0).
    spam_values = [d.spam_percent for d in donors if d.spam_percent is not None]

    def average(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    return Aggregate(
        count=len(donors),
        avg_dr=average(dr_values),
        avg_traffic=average(traffic_values),
        dr_sample=len(dr_values),
        traffic_sample=len(traffic_values),
        avg_outlinks=average(outlinks_values),
        avg_spam_percent=average(spam_values),
        outlinks_sample=len(outlinks_values),
        spam_sample=len(spam_values),
    )


def _language_addendum(dataset: Dataset, query: DonorQuery) -> LanguageAddendum | None:
    """Рахує мовний додаток для запиту про країну.

    Це і є те саме «без подвійного рахунку»: беремо донорів основною мовою
    країни й ВИКИДАЄМО тих, хто вже потрапив у ядро за доменною зоною.
    """
    country = query.country
    if country is None:
        return None

    language = country.language
    if language is None:
        return None

    zones = query.core_zones
    keys = language.data_keys

    count = sum(
        1
        for donor in dataset.donors
        if donor.language in keys  # потрібна мова
        and donor.zone not in zones  # але НЕ в зоні ядра — без подвійного рахунку
        and passes_metrics(donor, query)  # ті самі фільтри DR/трафіку
    )

    if count == 0:
        return None

    return LanguageAddendum(
        language=language,
        count=count,
        zone_label=country.zones_label,
        country_name=country.name_uk,
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
    if dataset.tracks_spam:
        return query
    return query.without(Dimension.OUTLINKS).without(Dimension.SPAM)


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

    query = normalize_query(dataset, query)
    core_donors = select_core(dataset, query)

    return QueryResult(
        section_title=dataset.title,
        query=query,
        core=aggregate(core_donors),
        # Мовний додаток — тільки для запиту про країну. Якщо користувач сам
        # указав і країну, і мову, додаток не потрібен: він уже все звузив.
        addendum=(_language_addendum(dataset, query) if query.kind is QueryKind.COUNTRY else None),
        zone_breakdown=zone_breakdown(core_donors) if with_breakdowns else (),
        language_breakdown=language_breakdown(core_donors) if with_breakdowns else (),
        country_breakdown=country_breakdown(core_donors) if with_breakdowns else (),
        total_in_base=dataset.count,
        tracks_spam=dataset.tracks_spam,
    )
