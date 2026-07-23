"""Складання повідомлень для Telegram.

Головне правило порядку в картці: МОВНИЙ РЯДОК ЗАВЖДИ ОСТАННІЙ.

Це не про красу. Зонове й мовне числа легко сплутати, а сплутавши —
пообіцяти клієнту те, чого немає. Тому мовний додаток фізично відділений
від решти картки: він іде після всіх приміток, окремим блоком, з іншим
значком і формулюванням «поза зоною».

Форматування — HTML (parse_mode="HTML"). Обрано замість Markdown навмисно:
у текстах повно крапок і підкреслень (.co.uk, n/a), і Markdown на них
постійно ламався б.
"""

from __future__ import annotations

import html
import time

from app.analytics.engine import Aggregate, QueryResult
from app.analytics.recommendations import Recommendations


def escape(text: str) -> str:
    """Готує текст до вставки в HTML-повідомлення.

    Екрануються лише «<», «>» і «&» — символи, які Telegram сприйняв би як
    розмітку. Лапки й апострофи лишаються як є: quote=True перетворив би
    апостроф на «&#x27;», і слова «м'якшими», «пам'яті», «В'єтнам» виглядали
    б у коді й логах як мотлох. У звичайному тексті екранувати їх нема
    потреби — вони небезпечні лише всередині атрибутів, а атрибутів у нас
    немає.
    """
    return html.escape(str(text), quote=False)


# Значок мовного рядка. Свідомо інший, ніж у решті картки, — щоб мовне
# число неможливо було переплутати із зоновим навіть побіжним поглядом.
LANGUAGE_MARK = "💬"


def plural_donors(count: int) -> str:
    """Правильна форма слова «донор» для числа.

    1 донор / 2 донори / 5 донорів — українська мова має три форми,
    і бот має говорити грамотно.
    """
    if count % 100 in (11, 12, 13, 14):
        return "донорів"
    last = count % 10
    if last == 1:
        return "донор"
    if last in (2, 3, 4):
        return "донори"
    return "донорів"


def number(value: float | None) -> str:
    """Число для людини: 3133.3 → «3 133», 32.0 → «32», None → «—»."""
    if value is None:
        return "—"
    rounded = round(value)
    # Пробіл між тисячами робить довгі числа читабельними.
    return f"{rounded:,}".replace(",", " ")


def _stale_note(as_of: float | None) -> str:
    """Помітка, що числа з кешу, бо онлайн-оновлення щойно не вдалося."""
    when = time.strftime("%d.%m %H:%M", time.localtime(as_of)) if as_of else "невідомо коли"
    return (
        f"🕓 <i>Онлайн-оновлення зараз недоступне (мережа). Показую збережені "
        f"числа станом на {when}.</i>"
    )


def percent(value: float | None) -> str:
    """Відсоток для людини: 62.5 → «63%», None → «—».

    Округлення half-up (а не банкове, як у round): 62.5 стає 63, як і
    очікує людина. Стандартний round дав би 62.
    """
    if value is None:
        return "—"
    return f"{int(value + 0.5)}%"


def _zeros_note(zeros: int) -> str:
    """Приписка «(з яких =0 — N)» — лише коли нулі справді є.

    Нуль входить у середнє й тягне його вниз, але на око його не видно.
    Порожні значення сюди НЕ рахуються — це різні речі: 0 є в даних, а
    порожнє — це «невідомо»."""
    return f" <i>(з яких =0 — {zeros})</i>" if zeros else ""


def _spam_distribution_line(distribution: tuple[tuple[str, int], ...]) -> str:
    """Рядок розподілу заспамленості: «12 (0), 26 (1-20), 45 (21-50)…».

    Перше число — скільки донорів у групі, у дужках — діапазон заспамлених
    лінків. Це РОЗПОДІЛ (а не середнє), тому й підпис інший."""
    if not distribution:
        return "🧪 <b>Заспамленість (донорів за к-стю лінків):</b> <i>немає даних</i>"
    parts = ", ".join(f"{number(count)} ({label})" for label, count in distribution)
    return f"🧪 <b>Заспамленість (донорів за к-стю лінків):</b> {parts}"


def _metrics_block(core: Aggregate, *, tracks_spam: bool = False) -> list[str]:
    """Рядки з показниками групи.

    Біля кожного середнього — приписка про нулі, коли вони є. Для «Морд»
    (tracks_spam=True) додаються вихідні лінки й РОЗПОДІЛ заспамленості за
    абсолютною кількістю. Для «Меджика» спам/вихідних немає.
    """
    lines = [
        f"📊 <b>Середній DR:</b> {number(core.avg_dr)}{_zeros_note(core.dr_zeros)}",
        f"📈 <b>Середній трафік:</b> {number(core.avg_traffic)}{_zeros_note(core.traffic_zeros)}",
    ]

    if core.avg_dr is None and core.avg_traffic is None:
        lines.append("<i>У цій групі не заповнені ні DR, ні трафік.</i>")

    if tracks_spam:
        lines.append(
            f"🔗 <b>Середня к-сть вихідних лінків:</b> "
            f"{number(core.avg_outlinks)}{_zeros_note(core.outlinks_zeros)}"
        )
        lines.append(_spam_distribution_line(core.spam_distribution))

    return lines


def _found_count(result: QueryResult) -> str:
    """Рядок «Знайдено донорів». Для запиту про країну — з розкладом складових:
    «5 (.fr 3 | мова 0 | GEO 2)». GEO-складову показуємо лише коли база її має."""
    split = result.split
    if split is None:
        return str(result.core.count)

    parts = [f"{escape(split.main_zone)} {split.zone}"]
    # Складову «мова» ховаємо для спільних мов — там її немає в підсумку.
    if split.show_language:
        parts.append(f"мова {split.language}")
    if split.show_geo:
        parts.append(f"GEO {split.geo}")
    return f"{split.total} ({' | '.join(parts)})"


def _error_note(core: Aggregate) -> str:
    """Один рядок про похибку: діапазон від нижньої межі до підсумку."""
    return (
        f"<i>Зверніть увагу: орієнтовна кількість з урахуванням похибки "
        f"{number(core.min_estimate)}–{number(core.count)} (допустима похибка 30%)</i>"
    )


def render_result(result: QueryResult, *, recommendations: Recommendations | None = None) -> str:
    """Головна картка результату.

    Порядок блоків:
      1. база і запит
      2. знайдено + похибка
      3. середні
      4. попередження
      5. примітка про похибку
      6. МОВНИЙ РЯДОК — завжди останній
    """
    if not result.available:
        return render_unavailable(result)

    core = result.core
    lines = [
        f"🗂 <b>База:</b> {escape(result.section_title)}",
        f"🔎 <b>Запит:</b> {escape(result.query.describe())}",
    ]

    # Помітка про застарілі дані — одразу під запитом, щоб її не проґавили.
    if result.stale:
        lines.append("")
        lines.append(_stale_note(result.as_of))

    lines.append("")
    lines.append(f"✅ <b>Знайдено донорів:</b> {_found_count(result)}")

    if core.count:
        lines.append("")
        lines.extend(_metrics_block(core, tracks_spam=result.tracks_spam))
    else:
        lines.append("")
        lines.append("За цими параметрами донорів не знайдено.")

    # -- попередження --------------------------------------------------------
    warnings: list[str] = []

    if core.count and core.low_sample:
        warnings.append(
            "⚠️ Середні розраховані менш ніж на трьох донорах — на такі значення краще не спиратися."
        )

    if core.weak_metrics:
        warnings.append(
            "⚠️ Показники групи низькі (середній DR або трафік менші за 3). "
            "Варто розширити фільтри або подивитися суміжні категорії."
        )

    if warnings:
        lines.append("")
        lines.extend(warnings)

    # -- примітка про похибку (один рядок) -----------------------------------
    if core.count:
        lines.append("")
        lines.append(_error_note(core))

    # -- рекомендації (коротким блоком, до мовних рядків) --------------------
    if recommendations is not None:
        extra = render_recommendations(recommendations)
        if extra:
            lines.append("")
            lines.append(extra)

    # -- МОВНІ РЯДКИ-ПРОПОЗИЦІЇ — ЗАВЖДИ ОСТАННІ -----------------------------
    for block in render_language_offers(result):
        lines.append("")
        lines.append(block)

    return "\n".join(lines)


def render_language_offers(result: QueryResult) -> list[str]:
    """Мовні рядки-пропозиції — окремі останні блоки картки.

    Їх може бути до двох, і жоден НЕ входить у підсумок:
      1. «на нейтральних зонах» — лише для спільних мов (крок (б), винесений
         із підсумку, щоб Британія не забирала всі .com-сайти);
      2. «на зонах інших країн» — мова країни на ccTLD інших країн.

    Для спільних мов до обох додається застереження «це не лише [країна]».
    """
    blocks: list[str] = []

    if result.neutral_offer is not None:
        offer = result.neutral_offer
        line = (
            f"{LANGUAGE_MARK} <b>{escape(offer.language.instrumental_uk)} "
            f"на нейтральних зонах — {number(offer.count)}</b> "
            f"<i>(це не лише {escape(offer.country_name)})</i>"
        )
        blocks.append(line)

    if result.addendum is not None:
        offer = result.addendum
        line = (
            f"{LANGUAGE_MARK} <b>{escape(offer.language.instrumental_uk)} "
            f"на зонах інших країн — {number(offer.count)}</b>"
        )
        if offer.needs_warning:
            # Спільна мова: нею пишуть багато країн, тому число не можна
            # читати як «ще стільки ж донорів саме цієї країни».
            line += f" <i>(це не лише {escape(offer.country_name)})</i>"
        blocks.append(line)

    return blocks


def render_recommendations(recommendations: Recommendations) -> str:
    """Блок «додатково можна розглянути»."""
    if recommendations.is_empty:
        return ""

    blocks: list[str] = []

    if recommendations.same_language:
        rows = "\n".join(
            f"  • {escape(s.label)} — {s.count}" for s in recommendations.same_language
        )
        blocks.append(f"🌍 <b>Суміжні країни з тією ж мовою:</b>\n{rows}")

    if recommendations.same_region:
        rows = "\n".join(f"  • {escape(s.label)} — {s.count}" for s in recommendations.same_region)
        blocks.append(f"🗺 <b>Суміжні гео регіону:</b>\n{rows}")

    if recommendations.relaxed:
        rows = "\n".join(f"  • {escape(s.label)} — {s.count}" for s in recommendations.relaxed)
        blocks.append(f"🔽 <b>Якщо пом'якшити вимоги:</b>\n{rows}")

    if recommendations.deficit is not None:
        hint = recommendations.deficit
        blocks.append(
            f"🎯 <b>Найбільше обмежує:</b> {escape(hint.filter_label)}\n"
            f"  Без цього фільтра було б {hint.without_filter_count} "
            f"замість {hint.current_count}."
        )

    if recommendations.reserve is not None:
        reserve = recommendations.reserve
        blocks.append(
            f"➕ <b>Ядро + запас:</b> {reserve.core_count} точно за запитом "
            f"і ще {reserve.reserve_count} {escape(reserve.reserve_label)}. "
            f"Разом до {reserve.total}."
        )

    return "\n\n".join(blocks)


def render_breakdown(title: str, rows: tuple[tuple[str, int], ...]) -> str:
    """Розподіл по групах: зони, мови, країни."""
    if not rows:
        return f"<b>{escape(title)}</b>\nДаних для розподілу немає."

    body = "\n".join(f"  • {escape(label)} — {count}" for label, count in rows)
    return f"<b>{escape(title)}</b>\n{body}"


def render_unavailable(result: QueryResult) -> str:
    """Повідомлення, коли база недоступна. Бот пояснює, а не мовчить."""
    reason = escape(result.error or "причина невідома")
    return (
        f"🗂 <b>База:</b> {escape(result.section_title)}\n\n"
        f"⚠️ База тимчасово недоступна.\n\n"
        f"<i>{reason}</i>\n\n"
        "Спробуйте ще раз за хвилину. Якщо не мине — покажіть це повідомлення адміну."
    )


def render_summary(result: QueryResult) -> str:
    """Короткий підсумок одним рядком — для списків і логів дій."""
    return (
        f"{result.section_title}: {result.core.count} {plural_donors(result.core.count)} "
        f"({result.query.describe()})"
    )
