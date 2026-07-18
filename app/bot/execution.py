"""Виконання запиту й показ результату.

Один спільний шлях для всіх способів запитати: кнопки, майстер і вільний
текст урешті приходять саме сюди. Так відповідь завжди має однаковий вигляд
і однакові правила безпеки.

Підрахунки виконуються в окремому потоці (asyncio.to_thread). У «Меджику»
близько 29 000 рядків, і хоч перебір швидкий, робити його прямо в основному
циклі не варто: поки бот рахує одному, він має відповідати іншим.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.analytics.engine import QueryResult, run_query
from app.analytics.query import DonorQuery
from app.analytics.recommendations import Recommendations, build_recommendations
from app.bot.context import BotServices
from app.bot.keyboards import back_to_menu, result_menu
from app.logging_setup import get_logger
from app.text.cards import render_result, render_summary

logger = get_logger(__name__)

STATUS_TEXT = "⏳ Рахую..."


@dataclass(frozen=True, slots=True)
class ExecutedQuery:
    """Готова відповідь: числа, підказки й текст картки."""

    result: QueryResult
    recommendations: Recommendations
    text: str


def _compute(dataset, query: DonorQuery) -> tuple[QueryResult, Recommendations]:
    """Синхронна частина підрахунків — виконується в окремому потоці."""
    return run_query(dataset, query), build_recommendations(dataset, query)


async def execute(services: BotServices, query: DonorQuery) -> ExecutedQuery:
    """Виконує запит і складає картку результату."""
    dataset = await services.repository.get(query.section_key)
    result, recommendations = await asyncio.to_thread(_compute, dataset, query)

    return ExecutedQuery(
        result=result,
        recommendations=recommendations,
        text=render_result(result, recommendations=recommendations),
    )


async def show_result(
    target: Message | CallbackQuery,
    services: BotServices,
    query: DonorQuery,
    user_id: int,
) -> ExecutedQuery:
    """Рахує запит і показує картку з кнопками.

    Спершу з'являється повідомлення «Рахую...», потім воно замінюється
    результатом — так користувач бачить, що бот працює (ТЗ, розділ 27).
    """
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    status = await message.answer(STATUS_TEXT)

    try:
        executed = await execute(services, query)
    except Exception:
        logger.exception("Не вдалося виконати запит")
        await status.edit_text(
            "⚠️ Не вдалося виконати запит. Спробуйте ще раз або почніть спочатку: /start",
            reply_markup=back_to_menu(),
        )
        raise

    # У журнал іде лише агрегований підсумок — доменів у ньому немає.
    services.action_log.add(user_id, render_summary(executed.result))

    await status.edit_text(
        executed.text,
        reply_markup=result_menu(
            query.section_key,
            has_recommendations=not executed.recommendations.is_empty,
        ),
    )
    return executed


async def safe_edit(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Замінює текст повідомлення, не падаючи на дрібницях.

    Telegram вважає помилкою спробу замінити текст на такий самий (це буває,
    коли двічі натиснути ту саму кнопку). Для користувача це не помилка,
    тому просто мовчки ігноруємо.
    """
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        # Інші причини (наприклад, повідомлення застаре для редагування) —
        # надсилаємо нове, щоб користувач усе одно побачив відповідь.
        await callback.message.answer(text, reply_markup=markup)
