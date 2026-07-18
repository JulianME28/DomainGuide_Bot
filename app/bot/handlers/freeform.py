"""Запити вільним текстом.

Цей обробник приймає будь-яке повідомлення, тому підключається ОСТАННІМ —
інакше він перехоплював би відповіді на кроках майстра.

Нейромережа не потрібна: країни й мови впізнає словник, числа — правила.
Якщо запит незрозумілий, бот чесно каже про це й показує приклади, а не
вигадує відповідь.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.context import BotServices
from app.bot.execution import show_result
from app.bot.keyboards import back_to_menu, cancel_only
from app.bot.states import Ask, query_to_state
from app.text.freeform import CLARIFICATION_TEXT, parse_free_text

router = Router(name="freeform")


@router.message(Command("query"))
async def cmd_query(message: Message, state: FSMContext) -> None:
    """Явний перехід у режим вільного запиту."""
    await state.set_state(Ask.free_text)
    await message.answer(
        "🤖 <b>Вільний запит</b>\n\nОпишіть, що потрібно. Наприклад:\n"
        "<code>Меджик, Британія, трафік від 1, DR не важливий</code>",
        reply_markup=cancel_only(),
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_free_text(message: Message, services: BotServices, state: FSMContext) -> None:
    """Будь-який текст поза кроками майстра розбирається як запит."""
    data = await state.get_data()
    parsed = parse_free_text(message.text or "", default_section=data.get("section_key", "magic"))

    if parsed.needs_clarification:
        await message.answer(CLARIFICATION_TEXT, reply_markup=back_to_menu())
        return

    await state.update_data(**query_to_state(parsed.query))
    await show_result(message, services, parsed.query, message.from_user.id)


@router.message()
async def handle_anything_else(message: Message) -> None:
    """Фото, стікери, файли — бот пояснює, що вміє тільки текст і кнопки."""
    await message.answer(
        "Я розумію текстові запити й кнопки.\n\nПочати: /start",
        reply_markup=back_to_menu(),
    )
