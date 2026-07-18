"""Команди /start, /help, головне меню й статус баз."""

from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.context import BotServices
from app.bot.execution import safe_edit
from app.bot.keyboards import back_to_menu, main_menu
from app.bot.states import query_from_state, summary_lines

router = Router(name="common")


WELCOME = (
    "👋 <b>Вітаю!</b>\n\n"
    "Я рахую донорів у ваших базах і повертаю зведені показники:\n"
    "кількість, середній DR, середній трафік, розподіли та рекомендації.\n\n"
    "<b>Оберіть базу для перевірки:</b>"
)

HELP = (
    "❓ <b>Як користуватися</b>\n\n"
    "<b>Найпростіше — кнопками.</b> Оберіть базу, потім країну чи мову — "
    "і бот усе порахує.\n\n"
    "<b>Можна й текстом:</b>\n"
    "• <code>Меджик, Британія, трафік від 1, DR не важливий</code>\n"
    "• <code>скільки німецькомовних донорів з DR від 20</code>\n"
    "• <code>Морди, .de, трафік від 100</code>\n\n"
    "<b>Команди:</b>\n"
    "/start — головне меню\n"
    "/help — ця довідка\n"
    "/magic — база Меджик\n"
    "/mordy — база Морди\n"
    "/submits — база Сабміти\n"
    "/query — запит вільним текстом\n"
    "/filters — які фільтри зараз активні\n"
    "/reset — скинути поточний запит\n"
    "/admin — адмін-меню\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🌍 <b>Найважливіше: країна й мова — це різні речі</b>\n\n"
    "У даних немає колонки країни, тому країну бот визначає за доменною "
    "зоною. На запит про Німеччину він показує донорів у зоні <code>.de</code>, "
    "а <b>окремим останнім рядком</b> — скільки є німецькомовних донорів "
    "<b>поза</b> цією зоною.\n\n"
    "⚠️ <b>Ці два числа не можна складати.</b> Частина донорів входить в "
    "обидві групи одночасно, тож сума була б завищеною. Це різні відповіді "
    "на різні питання, а не частини одного цілого.\n\n"
    "🔒 Бот повертає лише зведені показники. Повний список донорів і самі "
    "домени не видаються ніколи."
)


@router.message(CommandStart())
async def cmd_start(message: Message, services: BotServices, state: FSMContext) -> None:
    """Головне меню. Скидає поточний крок, але не запит."""
    await state.set_state(None)
    is_admin = services.settings.is_admin(message.from_user.id)
    await message.answer(WELCOME, reply_markup=main_menu(is_admin=is_admin))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=back_to_menu())


@router.message(Command("reset"))
async def cmd_reset(message: Message, services: BotServices, state: FSMContext) -> None:
    """Скидає поточний запит повністю (ТЗ, розділ 29)."""
    await state.clear()
    is_admin = services.settings.is_admin(message.from_user.id)
    await message.answer(
        "🔄 Поточний запит скинуто.\n\n<b>Оберіть базу:</b>",
        reply_markup=main_menu(is_admin=is_admin),
    )


@router.message(Command("filters"))
async def cmd_filters(message: Message, services: BotServices, state: FSMContext) -> None:
    """Показує активні фільтри."""
    data = await state.get_data()
    if not data:
        await message.answer(
            "Зараз активних фільтрів немає. Почніть запит: /start",
            reply_markup=back_to_menu(),
        )
        return

    query = query_from_state(data)
    await message.answer(
        summary_lines(query, services.section_title(query.section_key)),
        reply_markup=back_to_menu(),
    )


@router.callback_query(F.data == "menu:main")
async def show_main_menu(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    await state.set_state(None)
    is_admin = services.settings.is_admin(callback.from_user.id)
    await safe_edit(callback, WELCOME, main_menu(is_admin=is_admin))
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def show_help(callback: CallbackQuery) -> None:
    await safe_edit(callback, HELP, back_to_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:status")
async def show_status(callback: CallbackQuery, services: BotServices) -> None:
    """Статус баз — доступний усім дозволеним користувачам."""
    await safe_edit(callback, await build_status_text(services), back_to_menu())
    await callback.answer()


async def build_status_text(services: BotServices) -> str:
    """Складає текст про стан баз. Використовує і меню, і адмінка."""
    lines = ["📊 <b>Стан баз</b>", ""]

    for section in services.columns.sections.values():
        if not section.reads_data:
            lines.append(f"📩 <b>{section.title}</b> — розділ-заглушка, дані не читаються.")
            continue

        dataset = await services.repository.get(section.key)

        if not dataset.available:
            lines.append(f"⚠️ <b>{section.title}</b> — недоступна.")
            lines.append(f"   <i>{(dataset.error or '')[:200]}</i>")
            continue

        age = services.repository.age_seconds(section.key)
        age_text = f"{int(age // 60)} хв тому" if age and age >= 60 else "щойно"
        lines.append(f"✅ <b>{section.title}</b> — {dataset.count} донорів, оновлено {age_text}.")

        if dataset.rows_skipped:
            lines.append(f"   <i>Пропущено рядків без домену: {dataset.rows_skipped}</i>")
        if dataset.is_empty:
            lines.append("   <i>Аркуш порожній — це не помилка.</i>")

    lines.append("")
    lines.append(f"<i>Час перевірки: {time.strftime('%H:%M:%S')}</i>")
    return "\n".join(lines)
