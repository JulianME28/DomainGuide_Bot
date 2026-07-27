"""Індивідуальний запит: користувач пише вільною мовою, розбирає ЗАВЖДИ ШІ.

Це «розумний перекладач у фільтри», а НЕ вільний чат: ШІ бачить лише текст
запиту, перетворює його на структурований фільтр (базу, країну, метрики),
а backend валідує по whitelist і рахує по базі (ТЗ §5). Донорів ШІ не бачить
і відповіді не вигадує.

Відрізняється від звичайного вільного запиту (freeform) тим, що НЕ пробує
спершу словник — одразу йде в ШІ, навіть якщо словник зрозумів би сам. Сюди
ж веде кнопка «🧠 Уточнити через ШІ» під карткою «не зрозумів».
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.context import BotServices
from app.bot.execution import run_ai_query, safe_edit
from app.bot.keyboards import back_to_menu, cancel_only
from app.bot.states import Ask

router = Router(name="ai")

AI_PROMPT = (
    "🧠 <b>Індивідуальний запит</b>\n\n"
    "Опишіть, що потрібно, звичайною мовою — його розбере ШІ й перетворить на "
    "фільтр (база, країна, метрики), а бот порахує по базі.\n\n"
    "Наприклад:\n"
    "<code>мало заспамлені німецькі донори з непоганим трафіком</code>"
)

NO_TEXT_TO_RETRY = (
    "🧠 Немає запиту, який можна уточнити. Напишіть його ще раз або скористайтесь "
    "кнопками — /start."
)


@router.callback_query(F.data == "ai:start")
async def start_ai(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Індивідуальний запит» — просимо текст і чекаємо його в стані."""
    await state.set_state(Ask.ai_query)
    await safe_edit(callback, AI_PROMPT, cancel_only())
    await callback.answer()


@router.message(Ask.ai_query)
async def receive_ai_query(message: Message, services: BotServices, state: FSMContext) -> None:
    """Текст у режимі індивідуального запиту — ЗАВЖДИ через ШІ."""
    await run_ai_query(message, services, state, message.from_user.id, message.text or "")


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
