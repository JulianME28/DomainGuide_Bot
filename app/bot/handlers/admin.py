"""Адмін-меню: статус баз, карта колонок, тестовий запит, лог, оновлення даних.

Доступне лише тим, чий ID є в ADMIN_USER_IDS. Перевірка тут окрема й явна:
загальний захист пускає всіх дозволених користувачів, а адмін-функції — це
вужче коло.
"""

from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.analytics.query import DonorQuery
from app.bot.context import BotServices
from app.bot.execution import execute, safe_edit
from app.bot.handlers.common import build_status_text
from app.bot.keyboards import admin_menu, back_to_menu
from app.dictionary.countries import country_by_code

router = Router(name="admin")

ADMIN_MENU_TEXT = "⚙️ <b>Адмін-меню</b>\n\nСлужбова інформація й керування даними."

DENIED = "Ця дія доступна лише адміністратору."


def _is_admin(services: BotServices, user_id: int) -> bool:
    return services.settings.is_admin(user_id)


@router.message(Command("admin", "security"))
async def cmd_admin(message: Message, services: BotServices) -> None:
    if not _is_admin(services, message.from_user.id):
        await message.answer(DENIED)
        return
    await message.answer(ADMIN_MENU_TEXT, reply_markup=admin_menu())


@router.callback_query(F.data.startswith("admin:"))
async def admin_actions(callback: CallbackQuery, services: BotServices) -> None:
    """Усі кнопки адмінки в одному місці."""
    if not _is_admin(services, callback.from_user.id):
        await callback.answer(DENIED, show_alert=True)
        return

    action = callback.data.split(":")[1]

    if action == "menu":
        await safe_edit(callback, ADMIN_MENU_TEXT, admin_menu())

    elif action == "status":
        text = await build_status_text(services) + "\n\n" + _ai_status_text(services)
        await safe_edit(callback, text, admin_menu())

    elif action == "columns":
        await safe_edit(callback, _columns_text(services), admin_menu())

    elif action == "users":
        await safe_edit(callback, _users_text(services), admin_menu())

    elif action == "log":
        await safe_edit(callback, _log_text(services), admin_menu())

    elif action == "refresh":
        await callback.answer("Оновлюю...")
        await safe_edit(callback, "🔄 Перечитую дані з таблиці...", None)
        datasets = await services.repository.refresh()
        lines = ["🔄 <b>Дані оновлено</b>", ""]
        for dataset in datasets:
            if dataset.stale:
                # Оновлення не вдалося (мережа), лишилися попередні дані.
                when = time.strftime("%H:%M:%S", time.localtime(dataset.loaded_at))
                lines.append(
                    f"🕓 {dataset.title} — оновити не вдалося, показую кеш від {when} "
                    f"({dataset.count} донорів)"
                )
            elif dataset.available:
                lines.append(f"✅ {dataset.title} — {dataset.count} донорів")
            else:
                lines.append(f"⚠️ {dataset.title} — {(dataset.error or '')[:150]}")
        await safe_edit(callback, "\n".join(lines), admin_menu())
        return

    elif action == "test":
        await callback.answer("Виконую тестовий запит...")
        await safe_edit(callback, await _test_query_text(services), admin_menu())
        return

    else:
        await safe_edit(callback, ADMIN_MENU_TEXT, admin_menu())

    await callback.answer()


def _columns_text(services: BotServices) -> str:
    """Карта колонок — що з чим зв'язано."""
    lines = ["🗺 <b>Карта колонок</b>", ""]
    lines.append(f"<b>Whitelist:</b> {', '.join(sorted(services.columns.whitelist))}")
    lines.append("<i>Бот читає лише ці колонки. Інші — фізично недоступні.</i>")
    lines.append("")

    for section in services.columns.sections.values():
        lines.append(f"<b>{section.title}</b>")
        if not section.reads_data:
            lines.append("  <i>розділ-заглушка, дані не читаються</i>")
        else:
            lines.append(f"  аркуш: <code>{section.sheet}</code>")
            for role, header in section.columns.items():
                lines.append(f"  {role} → <code>{header}</code>")
            if not section.has_outlinks:
                lines.append("  <i>вихідних лінків немає — заспамленість вимкнена</i>")
        lines.append("")

    lines.append("<i>Колонки гео в даних немає: країна визначається за доменною зоною.</i>")
    return "\n".join(lines)


def _ai_status_text(services: BotServices) -> str:
    """Рядок статусу ШІ для адмінки: увімкнено/вимкнено, модель, виклики за день."""
    settings = services.settings
    if services.ai is None:
        state = "вимкнено"
        calls = 0
        hint = "щоб увімкнути — впишіть LLM_PROVIDER=anthropic і LLM_API_KEY у .env"
    else:
        state = "увімкнено"
        calls = services.ai.calls_today
        hint = "викликається лише коли словник не зрозумів запит"

    return (
        f"🤖 <b>ШІ (вільні запити):</b> {state}\n"
        f"провайдер: <code>{settings.llm_provider}</code>, "
        f"модель: <code>{settings.llm_model}</code>\n"
        f"викликів сьогодні: {calls}\n"
        f"<i>{hint}</i>"
    )


def _users_text(services: BotServices) -> str:
    """Список дозволених користувачів."""
    settings = services.settings
    allowed = ", ".join(str(i) for i in sorted(settings.allowed_user_ids)) or "порожньо"
    admins = ", ".join(str(i) for i in sorted(settings.admin_user_ids)) or "порожньо"

    return (
        "👥 <b>Доступ</b>\n\n"
        f"<b>Дозволені:</b> {allowed}\n"
        f"<b>Адміни:</b> {admins}\n\n"
        "<i>Список зберігається у файлі .env (ALLOWED_USER_IDS і ADMIN_USER_IDS), "
        "а не в коді. Щоб змінити — відредагуйте .env і перезапустіть бота.</i>"
    )


def _log_text(services: BotServices) -> str:
    """Журнал останніх дій."""
    records = services.action_log.recent(20)
    if not records:
        return "📜 <b>Лог порожній</b>\n\nЩе ніхто нічого не запитував."

    lines = ["📜 <b>Останні дії</b>", ""]
    lines.extend(f"<code>{r.time_text}</code> · {r.user_id} · {r.action}" for r in records)
    lines.append("")
    lines.append("<i>У журналі лише службові події та зведені числа. Доменів тут немає.</i>")
    return "\n".join(lines)


async def _test_query_text(services: BotServices) -> str:
    """Тестовий запит — перевірка, що весь ланцюжок працює."""
    query = DonorQuery(section_key="magic", country=country_by_code("de"))

    try:
        executed = await execute(services, query)
    except Exception as exc:
        return f"🧪 <b>Тестовий запит</b>\n\n⚠️ Помилка: {type(exc).__name__}: {exc}"

    result = executed.result
    if not result.available:
        return f"🧪 <b>Тестовий запит</b>\n\n⚠️ База недоступна:\n<i>{result.error}</i>"

    addendum = result.addendum
    addendum_text = (
        f"{addendum.count} {addendum.language.name_uk} поза зоною" if addendum else "немає"
    )

    return (
        "🧪 <b>Тестовий запит</b>\n\n"
        f"Запит: {query.describe()}\n\n"
        f"Ядро (зона): <b>{result.core.count}</b>\n"
        f"Мовний додаток: <b>{addendum_text}</b>\n"
        f"Середній DR: {result.core.avg_dr}\n"
        f"Середній трафік: {result.core.avg_traffic}\n"
        f"Усього в базі: {result.total_in_base}\n\n"
        "<i>Якщо числа виглядають розумно — ланцюжок «Google → розбір → "
        "підрахунки → картка» працює.</i>"
    )


@router.message(Command("status"))
async def cmd_status(message: Message, services: BotServices) -> None:
    """Статус баз доступний усім дозволеним користувачам."""
    await message.answer(await build_status_text(services), reply_markup=back_to_menu())
