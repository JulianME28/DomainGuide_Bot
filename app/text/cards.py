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


def _metrics_block(core: Aggregate) -> list[str]:
    """Рядки з середніми показниками."""
    lines = [
        f"📊 <b>Середній DR:</b> {number(core.avg_dr)}",
        f"📈 <b>Середній трафік:</b> {number(core.avg_traffic)}",
    ]

    if core.avg_dr is None and core.avg_traffic is None:
        lines.append("<i>У цій групі не заповнені ні DR, ні трафік.</i>")

    return lines


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
        "",
        f"✅ <b>Знайдено донорів:</b> {core.count}",
    ]

    if core.count:
        lines.append(
            f"📐 <b>Орієнтовна кількість з урахуванням похибки:</b> "
            f"від {core.min_estimate} до {core.count}"
        )
        lines.append("")
        lines.extend(_metrics_block(core))
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

    # -- примітка про похибку ------------------------------------------------
    if core.count:
        lines.append("")
        lines.append(
            "<i>Зверніть увагу: підсумкова кількість є орієнтовною. Допустима похибка — до 30%.</i>"
        )

    # -- рекомендації (коротким блоком, до мовного рядка) --------------------
    if recommendations is not None:
        extra = render_recommendations(recommendations)
        if extra:
            lines.append("")
            lines.append(extra)

    # -- МОВНИЙ РЯДОК — ЗАВЖДИ ОСТАННІЙ --------------------------------------
    language_block = render_language_addendum(result)
    if language_block:
        lines.append("")
        lines.append(language_block)

    return "\n".join(lines)


def render_language_addendum(result: QueryResult) -> str:
    """Мовний додаток — окремий останній блок картки.

    Читається однозначно: «крім донорів у зоні .de, є ще N донорів
    німецькою мовою поза цією зоною». Із головним числом не сумується
    і поруч із ним не стоїть.
    """
    addendum = result.addendum
    if addendum is None:
        return ""

    line = (
        f"{LANGUAGE_MARK} <b>+ {addendum.count} {plural_donors(addendum.count)} "
        f"{escape(addendum.language.instrumental_uk)} мовою поза зоною "
        f"{escape(addendum.zone_label)}</b>"
    )

    if addendum.needs_warning:
        # Спільні мови (en, es, pt, ar): цією мовою пишуть у багатьох країнах,
        # тому мовне число не можна читати як «ще стільки ж донорів країни».
        line += (
            f"\n⚠️ <i>{escape(addendum.language.instrumental_uk)} пишуть багато країн, "
            f"не лише {escape(addendum.country_name)}.</i>"
        )

    return line


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
            f"і ще {reserve.reserve_count} — {escape(reserve.reserve_label)}. "
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
