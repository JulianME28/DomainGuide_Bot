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
from aiogram.types import InlineKeyboardMarkup, Message

from app.bot.context import BotServices
from app.bot.execution import (
    AI_CHAT_FAILED_TEXT,
    chat_reply,
    reset_chat_history,
    resolve_with_ai,
    run_ai_query,
    show_both_bases,
    show_country_breakdown,
    show_result,
)
from app.bot.keyboards import ai_retry_menu, back_to_menu, cancel_only
from app.bot.states import Ask, query_to_state
from app.text.cards import escape, render_not_understood
from app.text.freeform import (
    BASE_CLARIFICATION_TEXT,
    CLARIFICATION_TEXT,
    EMPTY_QUERY_HINT,
    parse_free_text,
)

router = Router(name="freeform")


def _not_understood_menu(services: BotServices) -> InlineKeyboardMarkup:
    """Клавіатура під карткою «не зрозумів»: з кнопкою ШІ, лише коли ШІ ввімкнено."""
    return ai_retry_menu() if services.ai is not None else back_to_menu()


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
    text = message.text or ""
    parsed = parse_free_text(text, default_section=data.get("section_key", "magic"))
    # Запам'ятовуємо текст запиту — щоб кнопка «Уточнити через ШІ» могла
    # повторити РІВНО ТОЙ САМИЙ запит через ШІ.
    await state.update_data(last_text=text)

    if parsed.ambiguous_bases:
        await message.answer(BASE_CLARIFICATION_TEXT, reply_markup=back_to_menu())
        return

    # Запит-ПОКРИТТЯ («потреба по країнах + бракує/вистачає/покриття») словник
    # порахував би хибно (взяв би один поріг за глобальний traffic-фільтр). Тож
    # для переліку країн віддаємо його ШІ: він РОЗКЛАДАЄ на операцію coverage, а
    # рахує рушій (run_ai_query сам обробляє операцію й має власний фолбек).
    if parsed.wants_coverage and parsed.query.is_multi_country and services.ai is not None:
        await run_ai_query(message, services, state, message.from_user.id, text)
        return

    if parsed.needs_clarification:
        # Словник не зрозумів — пробуємо ШІ (якщо ввімкнено). Не вийшло — підказка.
        outcome = await resolve_with_ai(services, message.from_user.id, text)
        if outcome is not None and outcome.query is not None:
            await state.update_data(**query_to_state(outcome.query))
            await show_result(message, services, outcome.query, message.from_user.id)
            return
        # Фільтра нема, але це РОЗМОВНЕ питання → консультант (без доступу до даних),
        # з контекстом розмови у FSM.
        if outcome is not None and outcome.intent == "question":
            answer = await chat_reply(services, state, message.from_user.id, text)
            await message.answer(
                f"🧠 {escape(answer)}" if answer else AI_CHAT_FAILED_TEXT,
                reply_markup=back_to_menu(),
            )
            return
        # ШІ не допоміг. Якщо у запиті БУЛИ слова-претензії на параметр — кажемо
        # прямо, чого не зрозуміли, а не глухе «не розпізнав».
        if parsed.unrecognized:
            await message.answer(
                render_not_understood(parsed.unrecognized),
                reply_markup=_not_understood_menu(services),
            )
            return
        await message.answer(CLARIFICATION_TEXT, reply_markup=back_to_menu())
        return

    # Новий вільний запит замінює попередній повністю: свіжим вважається
    # рівно те, про що сказали в цьому повідомленні. Донор-запит скидає контекст
    # консультанта — розмова й підрахунок не змішуються.
    await state.update_data(**query_to_state(parsed.query, parsed.mentioned))
    await reset_chat_history(state)

    # Порожній запит без валідного фільтра — базу НЕ вивалюємо.
    if not parsed.query.is_multi_country and parsed.query.is_empty:
        # Були слова-претензії, але жодну не розпізнали → чесний 0 + «не зрозумів»
        # (навіть коли названо базу — фальшиве «правдиве» число небезпечніше).
        if parsed.unrecognized:
            await message.answer(
                render_not_understood(parsed.unrecognized),
                reply_markup=_not_understood_menu(services),
            )
            return
        # Справді без параметрів і без бази → підказка-уточнення. Названу базу
        # без фільтра лишаємо як є (агрегат по всій базі — це свідомий вибір).
        if not parsed.section_named:
            await message.answer(EMPTY_QUERY_HINT, reply_markup=back_to_menu())
            return

    # Розбивка по країнах на запит зі словом-сигналом («які країни», «по країнах»)
    # без переліку конкретних країн: топ-N + «Разом» + «…та ще N» + кнопка «всі».
    if parsed.wants_country_breakdown and not parsed.query.is_multi_country:
        await show_country_breakdown(
            message,
            services,
            parsed.query,
            message.from_user.id,
            both=parsed.both_bases or not parsed.section_named,
            # Словниковий/вільний розбір → кнопка «Уточнити через ШІ» на картці.
            ai_refine=True,
        )
        return

    # Зведення по обох базах, коли базу не назвали ЯВНО, або коли її назвали
    # переліком («(Меджик + Морди)», «в обох базах»). Список країн БІЛЬШЕ не виняток
    # (пункт III): show_both_bases сам рахує мультикраїнну модель на кожну базу.
    show_both = parsed.both_bases or not parsed.section_named
    if show_both:
        await show_both_bases(
            message,
            services,
            parsed.query,
            message.from_user.id,
            explicit_both=parsed.both_bases,
            ai_refine=True,
        )
        return
    await show_result(message, services, parsed.query, message.from_user.id, ai_refine=True)


@router.message()
async def handle_anything_else(message: Message) -> None:
    """Фото, стікери, файли — бот пояснює, що вміє тільки текст і кнопки."""
    await message.answer(
        "Я розумію текстові запити й кнопки.\n\nПочати: /start",
        reply_markup=back_to_menu(),
    )
