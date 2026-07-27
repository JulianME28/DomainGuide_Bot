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

from app.analytics.engine import (
    MAX_MULTI_COUNTRIES,
    QueryResult,
    run_multi_country,
    run_query,
)
from app.analytics.query import Dimension, DonorQuery
from app.analytics.recommendations import (
    Recommendations,
    build_recommendations,
    list_neighbours,
)
from app.bot.context import BotServices
from app.bot.keyboards import back_to_menu, both_bases_menu, result_menu
from app.bot.states import query_to_state
from app.logging_setup import get_logger
from app.text.cards import (
    render_both_bases,
    render_compact_block,
    render_multi_country,
    render_multi_summary,
    render_result,
    render_summary,
)

logger = get_logger(__name__)

STATUS_TEXT = "⏳ Рахую..."


@dataclass(frozen=True, slots=True)
class ExecutedQuery:
    """Готова відповідь: числа, підказки й текст картки."""

    result: QueryResult
    recommendations: Recommendations
    text: str
    alt_base: tuple[str, str] | None = None
    """(ключ, назва) бази, де є відкинуті виміри — для кнопки «виконати там»."""


def _compute(dataset, query: DonorQuery) -> tuple[QueryResult, Recommendations]:
    """Синхронна частина підрахунків — виконується в окремому потоці."""
    return run_query(dataset, query), build_recommendations(dataset, query)


def _alt_base_for(services: BotServices, result: QueryResult) -> tuple[str, str] | None:
    """База, яка МАЄ всі відкинуті виміри запиту (щоб запустити запит там).

    None, якщо відкинутих вимірів немає або жодна інша база їх не покриває.
    Наприклад, для фільтра заспамленості в «Меджику» поверне ключ і назву «Морд».
    """
    dropped = result.dropped_dimensions
    if not dropped:
        return None

    for section in services.columns.sections.values():
        if not section.reads_data or section.key == result.query.section_key:
            continue
        if _supports_all(section, dropped):
            return section.key, section.title
    return None


def _supports_all(section, dimensions: frozenset[str]) -> bool:
    """Чи має розділ колонки для ВСІХ цих вимірів."""
    for dimension in dimensions:
        if dimension == Dimension.SPAM and not section.tracks_spam:
            return False
        if dimension == Dimension.OUTLINKS and not section.has_outlinks:
            return False
        if dimension == Dimension.GEO and not section.has_geo:
            return False
    return True


async def execute(
    services: BotServices, query: DonorQuery, *, ai_explained: bool = False
) -> ExecutedQuery:
    """Виконує запит і складає картку результату."""
    dataset = await services.repository.get(query.section_key)
    result, recommendations = await asyncio.to_thread(_compute, dataset, query)

    alt_base = _alt_base_for(services, result)
    return ExecutedQuery(
        result=result,
        recommendations=recommendations,
        text=render_result(
            result,
            recommendations=recommendations,
            dropped_alt_base=alt_base[1] if alt_base else None,
            ai_explained=ai_explained,
        ),
        alt_base=alt_base,
    )


def data_bases(services: BotServices) -> list[tuple[str, str]]:
    """Бази, які реально читають дані: (ключ, назва). «Сабміти»-заглушку пропускаємо."""
    return [
        (section.key, section.title)
        for section in services.columns.sections.values()
        if section.reads_data
    ]


async def show_both_bases(
    target: Message | CallbackQuery,
    services: BotServices,
    query: DonorQuery,
    user_id: int,
    *,
    explicit_both: bool = False,
) -> None:
    """Зведений показ по ОБОХ базах — коли базу в запиті не назвали.

    Кожна база — окремим компактним блоком (без великих додаткових блоків),
    унизу кнопки «Детально по …» на повну картку відповідної бази.

    Спільного підсумкового рядка «Загалом» поки НЕМАЄ: питання «унікальні vs
    проста сума» ще відкрите, тож показуємо лише блоки по кожній базі."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    bases = data_bases(services)
    status = await message.answer(STATUS_TEXT)

    try:
        blocks: list[str] = []
        for key, _title in bases:
            per_base = query.replace(section_key=key)
            dataset = await services.repository.get(key)
            # Без розподілів — компактному блоку вони не потрібні.
            result = await asyncio.to_thread(run_query, dataset, per_base, with_breakdowns=False)
            alt = _alt_base_for(services, result)
            blocks.append(render_compact_block(result, dropped_alt_base=alt[1] if alt else None))
    except Exception:
        logger.exception("Не вдалося виконати запит по обох базах")
        await status.edit_text(
            "⚠️ Не вдалося виконати запит. Спробуйте ще раз або почніть спочатку: /start",
            reply_markup=back_to_menu(),
        )
        return

    # У журнал — лише зведений опис запиту, без доменів.
    services.action_log.add(user_id, f"обидві бази: {query.describe()}")
    await status.edit_text(
        render_both_bases(query, blocks, explicit_both=explicit_both),
        reply_markup=both_bases_menu(bases),
    )


async def show_result(
    target: Message | CallbackQuery,
    services: BotServices,
    query: DonorQuery,
    user_id: int,
    *,
    ai_explained: bool = False,
) -> ExecutedQuery | None:
    """Рахує запит і показує картку з кнопками.

    Спершу з'являється повідомлення «Рахую...», потім воно замінюється
    результатом — так користувач бачить, що бот працює (ТЗ, розділ 27).

    Запит по СПИСКУ країн має інший вигляд відповіді (розклад по країнах +
    унікальний підсумок), тому йде окремим шляхом. `ai_explained` — чи фільтр
    склав ШІ (тоді картка підписує рядок запиту «ШІ зрозумів як»)."""
    if query.is_multi_country:
        await show_multi_country(target, services, query, user_id, ai_explained=ai_explained)
        return None

    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    status = await message.answer(STATUS_TEXT)

    try:
        executed = await execute(services, query, ai_explained=ai_explained)
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
            has_country=query.country is not None,
            run_in=executed.alt_base,
        ),
    )
    return executed


async def show_multi_country(
    target: Message | CallbackQuery,
    services: BotServices,
    query: DonorQuery,
    user_id: int,
    *,
    ai_explained: bool = False,
) -> None:
    """Рахує й показує запит по СПИСКУ країн: розклад + унікальний підсумок."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    if len(query.countries) > MAX_MULTI_COUNTRIES:
        await message.answer(
            f"У списку забагато країн ({len(query.countries)}). За один запит — "
            f"не більше {MAX_MULTI_COUNTRIES}. Зменшіть список і спробуйте ще раз.",
            reply_markup=back_to_menu(),
        )
        return

    status = await message.answer(STATUS_TEXT)

    try:
        dataset = await services.repository.get(query.section_key)
        result, suggestions = await asyncio.to_thread(_compute_multi, dataset, query)
    except Exception:
        logger.exception("Не вдалося виконати запит по списку країн")
        await status.edit_text(
            "⚠️ Не вдалося виконати запит. Спробуйте ще раз або почніть спочатку: /start",
            reply_markup=back_to_menu(),
        )
        return

    # У журнал — лише зведене число, без доменів.
    services.action_log.add(user_id, render_multi_summary(result))
    await status.edit_text(
        render_multi_country(result, suggestions=suggestions, ai_explained=ai_explained),
        reply_markup=back_to_menu(),
    )


# Суміжні країни показуємо лише для КОРОТКОГО списку — інакше блок задовгий.
MULTI_SUGGESTIONS_LIMIT = 7


def _compute_multi(dataset, query: DonorQuery):
    """Синхронна частина мультизапиту: підрахунок + суміжні (в окремому потоці)."""
    result = run_multi_country(dataset, query, unrecognized=query.unrecognized)
    # Суміжні — лише коли країн МЕНШЕ 7 (2..6): для довшого списку це шум.
    suggestions = (
        list_neighbours(dataset, query) if len(query.countries) < MULTI_SUGGESTIONS_LIMIT else ()
    )
    return result, suggestions


async def resolve_with_ai(services: BotServices, user_id: int, text: str) -> DonorQuery | None:
    """Резервний розбір через ШІ — лише коли ШІ ввімкнено (є ключ).

    Викликається ТІЛЬКИ якщо детермінований словниковий розбір не зрозумів
    запит. Будь-яка проблема (вимкнено, ліміт, помилка, таймаут) → None, і
    викликач показує звичайну підказку. ШІ бачить лише текст — не дані."""
    if services.ai is None:
        return None
    return await services.ai.try_interpret(user_id, text)


AI_DISABLED_TEXT = (
    "🧠 <b>ШІ зараз вимкнено.</b>\n\n"
    "Індивідуальний запит потребує ключа ШІ у налаштуваннях. Поки що скористайтеся "
    "звичайним запитом або кнопками меню — /start."
)

AI_FAILED_TEXT = (
    "🧠 <b>ШІ не зміг обробити запит.</b>\n\n"
    "Можливо, він тимчасово недоступний або вичерпано ліміт запитів. Спробуйте "
    "трохи згодом або скористайтеся кнопками меню — /start."
)

AI_STATUS_TEXT = "🧠 Питаю ШІ..."


async def run_ai_query(
    target: Message | CallbackQuery,
    services: BotServices,
    state,
    user_id: int,
    text: str,
) -> None:
    """Розбирає текст ЧЕРЕЗ ШІ й показує картку з підписом «ШІ зрозумів як…».

    ШІ лише перекладає текст у фільтр (базу/країну/метрики); валідація по
    whitelist і підрахунок — як завжди (ТЗ §5, донорів ШІ не бачить). Ліміт і
    лічильник викликів застосовуються самі (через AIService.try_interpret).
    Будь-яка проблема з ШІ → зрозуміле повідомлення, без падіння."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    if services.ai is None:
        await message.answer(AI_DISABLED_TEXT, reply_markup=back_to_menu())
        return

    status = await message.answer(AI_STATUS_TEXT)
    query = await resolve_with_ai(services, user_id, text.strip())
    if query is None:
        await status.edit_text(AI_FAILED_TEXT, reply_markup=back_to_menu())
        return

    # Розпізнане ШІ стає активним запитом (як звичайний), стан скидаємо.
    await state.set_state(None)
    await state.update_data(**query_to_state(query))
    # Прибираємо статус «Питаю ШІ...» і показуємо картку окремим повідомленням.
    await _delete_or_ignore(status)
    await show_result(message, services, query, user_id, ai_explained=True)


async def _delete_or_ignore(status) -> None:
    """Прибирає тимчасове повідомлення «Питаю ШІ...», не падаючи на дрібницях."""
    try:
        await status.delete()
    except Exception:
        logger.debug("Не вдалося видалити статус-повідомлення ШІ", exc_info=True)


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
