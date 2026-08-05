"""ШІ-режими: індивідуальний запит і живий консультант.

Це «розумний перекладач у фільтри», а НЕ вільний чат: ШІ бачить лише текст
запиту, перетворює його на структурований фільтр (базу, країну, метрики),
а backend валідує по whitelist і рахує по базі (ТЗ §5). Донорів ШІ не бачить
і відповіді не вигадує.

Відрізняється від звичайного вільного запиту (freeform) тим, що НЕ пробує
спершу словник — одразу йде в ШІ, навіть якщо словник зрозумів би сам. Сюди
ж веде кнопка «🧠 Уточнити через ШІ» під карткою «не зрозумів».

Живий консультант — окремий багатоходовий чат. Він не бачить даних бази,
але пояснює, радить і пам’ятає контекст останніх реплік.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.context import BotServices
from app.bot.execution import (
    AI_CHAT_FAILED_TEXT,
    chat_reply,
    reset_chat_history,
    run_ai_query,
    safe_edit,
)
from app.bot.keyboards import ai_chat_menu, back_to_menu, cancel_only
from app.bot.states import Ask
from app.text.cards import escape

router = Router(name="ai")

AI_PROMPT = (
    "🧠 <b>Індивідуальний запит</b>\n\n"
    "Опишіть будь-яку стандартну чи нестандартну умову. ШІ спробує "
    "зрозуміти мету, перетворити її на дозволений фільтр/операцію, "
    "а якщо даних бракує — поставити уточнювальне питання. Рахує завжди bot, "
    "тому ШІ не вигадує чисел.\n\n"
    "Наприклад:\n"
    "<code>мало заспамлені німецькі донори з непоганим трафіком</code>"
)

AI_CHAT_PROMPT = (
    "💬 <b>ШІ-консультант</b>\n\n"
    "Це живий діалог: запитуйте пояснення, порівняння, стратегію чи "
    "рекомендацію. Я пам’ятаю останні репліки й можу ставити уточнювальні питання.\n\n"
    "🔒 Консультант не бачить Google Sheets і домени, тому не вигадує цифри. "
    "Для точного підрахунку використайте «Індивідуальний запит» або /ai."
)

NO_TEXT_TO_RETRY = (
    "🧠 Немає запиту, який можна уточнити. Напишіть його ще раз або скористайтесь "
    "кнопками — /start."
)


@router.callback_query(F.data == "ai:start")
async def start_ai(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Індивідуальний запит» — просимо текст і чекаємо його в стані."""
    await reset_chat_history(state)
    await state.update_data(ai_pending_text="")
    await state.set_state(Ask.ai_query)
    await safe_edit(callback, AI_PROMPT, cancel_only())
    await callback.answer()


@router.message(Command("ai"))
async def cmd_ai(message: Message, state: FSMContext) -> None:
    """Команда для того самого режиму, що й кнопка «Індивідуальний запит»."""
    await reset_chat_history(state)
    await state.update_data(ai_pending_text="")
    await state.set_state(Ask.ai_query)
    await message.answer(AI_PROMPT, reply_markup=cancel_only())


@router.callback_query(F.data == "ai:chat")
async def start_ai_chat(callback: CallbackQuery, state: FSMContext) -> None:
    """Вхід у явний багатоходовий режим консультанта."""
    await reset_chat_history(state)
    await state.set_state(Ask.ai_chat)
    await safe_edit(callback, AI_CHAT_PROMPT, ai_chat_menu())
    await callback.answer()


@router.message(Command("chat"))
async def cmd_ai_chat(message: Message, services: BotServices, state: FSMContext) -> None:
    if services.ai is None:
        await message.answer("🧠 ШІ зараз вимкнено.", reply_markup=back_to_menu())
        return
    await reset_chat_history(state)
    await state.set_state(Ask.ai_chat)
    await message.answer(AI_CHAT_PROMPT, reply_markup=ai_chat_menu())


@router.callback_query(F.data == "ai:chat:new")
async def new_ai_chat(callback: CallbackQuery, state: FSMContext) -> None:
    await reset_chat_history(state)
    await state.set_state(Ask.ai_chat)
    await safe_edit(callback, AI_CHAT_PROMPT, ai_chat_menu())
    await callback.answer("Контекст діалогу очищено")


@router.message(Ask.ai_chat)
async def receive_ai_chat(message: Message, services: BotServices, state: FSMContext) -> None:
    """У режимі чату не запускаємо JSON-класифікатор: текст одразу йде консультанту."""
    text = message.text or ""
    if services.ai is None:
        await message.answer("🧠 ШІ зараз вимкнено.", reply_markup=back_to_menu())
        return
    if not text.strip():
        await message.answer("Напишіть питання текстом.", reply_markup=ai_chat_menu())
        return
    answer = await chat_reply(services, state, message.from_user.id, text.strip())
    await message.answer(
        f"🧠 {escape(answer)}" if answer else AI_CHAT_FAILED_TEXT,
        reply_markup=ai_chat_menu(),
    )


@router.message(Ask.ai_query)
async def receive_ai_query(message: Message, services: BotServices, state: FSMContext) -> None:
    """Текст у режимі індивідуального запиту — ЗАВЖДИ через ШІ."""
    text = message.text or ""
    data = await state.get_data()
    pending = str(data.get("ai_pending_text") or "").strip()
    full_text = f"{pending}\nУточнення користувача: {text}" if pending else text
    await run_ai_query(message, services, state, message.from_user.id, full_text)


@router.callback_query(F.data == "ai:retry")
async def retry_via_ai(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """Кнопка «Уточнити через ШІ» — повторний розбір ТОГО САМОГО запиту через ШІ."""
    data = await state.get_data()
    text = data.get("last_text", "")
    await callback.answer()
    if not text.strip():
        if callback.message:
            await callback.message.answer(NO_TEXT_TO_RETRY, reply_markup=back_to_menu())
        return
    await run_ai_query(callback, services, state, callback.from_user.id, text)
