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
from dataclasses import dataclass, replace

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.analytics.coverage import run_coverage
from app.analytics.engine import (
    MAX_MULTI_COUNTRIES,
    QueryResult,
    country_distribution,
    run_multi_country,
    run_query,
    unsupported_dimensions,
)
from app.analytics.query import ComparisonQuery, CoverageQuery, Dimension, DonorQuery
from app.analytics.recommendations import (
    Recommendations,
    build_recommendations,
    list_neighbours,
)
from app.bot.context import BotServices
from app.bot.keyboards import (
    ai_retry_menu,
    back_to_menu,
    both_bases_menu,
    result_menu,
    stop_check_menu,
)
from app.bot.states import Ask, query_to_state
from app.llm.service import AIOutcome
from app.logging_setup import get_logger
from app.text.cards import (
    escape,
    render_both_bases,
    render_compact_block,
    render_compact_multi_block,
    render_comparison,
    render_country_breakdown,
    render_country_breakdown_block,
    render_coverage,
    render_multi_country,
    render_multi_summary,
    render_result,
    render_summary,
)
from app.text.freeform import BASE_CLARIFICATION_TEXT, parse_free_text

logger = get_logger(__name__)

STATUS_TEXT = "⏳ Рахую..."

# Скільки країн показувати в розбивці за замовчуванням (решта — за кнопкою
# «Показати всі країни»). Параметр: змінити тут — і зміниться всюди.
COUNTRY_BREAKDOWN_TOP_N = 8


@dataclass(frozen=True, slots=True)
class ExecutedQuery:
    """Готова відповідь: числа, підказки й текст картки."""

    result: QueryResult
    recommendations: Recommendations
    text: str
    alt_base: tuple[str, str] | None = None
    """(ключ, назва) бази, де є відкинуті виміри — для кнопки «виконати там»."""


def _wants_ai_retry(
    services: BotServices, query: DonorQuery, *, ai_explained: bool, ai_refine: bool
) -> bool:
    """Чи додавати кнопку «🧠 Уточнити через ШІ» під картку результату.

    Правило: кнопка є на КОЖНІЙ словниковій/вільній картці (коли ШІ ввімкнено) —
    завжди для вільного тексту (`ai_refine`) й ОБОВʼЯЗКОВО, коли щось не впізнано
    (`query.unrecognized`, напр. «не впізнав як країну»). Це головний фікс: навіть
    коли словник не розпізнав coverage, кнопка дає переграти через ШІ.

    НЕ дублюємо на картці, що вже пройшла ШІ (`ai_explained`) — там уточнювати
    нема сенсу, результат уже від ШІ."""
    if services.ai is None or ai_explained:
        return False
    return ai_refine or bool(query.unrecognized)


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
        if dimension == Dimension.GEO and not section.has_geo:
            return False
    return True


def _alt_base_title_for(
    services: BotServices, dropped: frozenset[str], current_key: str
) -> str | None:
    """Назва бази, яка МАЄ всі відкинуті виміри — для мультикраїнного блоку.

    Те саме, що _alt_base_for, але від готового набору `dropped` (бо
    MultiCountryResult не несе dropped_dimensions). Повертає лише назву."""
    if not dropped:
        return None
    for section in services.columns.sections.values():
        if not section.reads_data or section.key == current_key:
            continue
        if _supports_all(section, dropped):
            return section.title
    return None


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
    ai_explained: bool = False,
    ai_refine: bool = False,
) -> None:
    """Зведений показ по ОБОХ базах — коли базу в запиті не назвали.

    Кожна база — окремим компактним блоком (без великих додаткових блоків),
    унизу кнопки «Детально по …» на повну картку відповідної бази.

    Спільного підсумкового рядка «Загалом» поки НЕМАЄ: питання «унікальні vs
    проста сума» ще відкрите, тож показуємо лише блоки по кожній базі."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    # Запит по СПИСКУ країн рахуємо мультикраїнною моделлю НА КОЖНУ базу (пункт III).
    is_multi = query.is_multi_country
    if is_multi and len(query.countries) > MAX_MULTI_COUNTRIES:
        await message.answer(
            f"У списку забагато країн ({len(query.countries)}). За один запит — "
            f"не більше {MAX_MULTI_COUNTRIES}. Зменшіть список і спробуйте ще раз.",
            reply_markup=back_to_menu(),
        )
        return

    bases = data_bases(services)
    status = await message.answer(STATUS_TEXT)

    try:
        blocks: list[str] = []
        for key, _title in bases:
            per_base = query.replace(section_key=key)
            dataset = await services.repository.get(key)
            if is_multi:
                # На кожну базу — ексклюзивний розклад по країнах. dropped: виміри,
                # яких база не має (напр. заспамленість у Меджику) — чесно попереджаємо.
                result = await asyncio.to_thread(
                    run_multi_country, dataset, per_base, unrecognized=per_base.unrecognized
                )
                dropped = unsupported_dimensions(dataset, per_base)
                blocks.append(
                    render_compact_multi_block(
                        result,
                        dropped=dropped,
                        dropped_alt_base=_alt_base_title_for(services, dropped, key),
                    )
                )
            else:
                # Одна країна: звичайний компактний блок (без розподілів).
                result = await asyncio.to_thread(
                    run_query, dataset, per_base, with_breakdowns=False
                )
                alt = _alt_base_for(services, result)
                blocks.append(
                    render_compact_block(result, dropped_alt_base=alt[1] if alt else None)
                )
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
        render_both_bases(
            query,
            blocks,
            explicit_both=explicit_both,
            ai_explained=ai_explained,
        ),
        reply_markup=both_bases_menu(
            bases,
            ai_retry=_wants_ai_retry(
                services, query, ai_explained=ai_explained, ai_refine=ai_refine
            ),
        ),
    )


async def show_result(
    target: Message | CallbackQuery,
    services: BotServices,
    query: DonorQuery,
    user_id: int,
    *,
    ai_explained: bool = False,
    ai_refine: bool = False,
) -> ExecutedQuery | None:
    """Рахує запит і показує картку з кнопками.

    Спершу з'являється повідомлення «Рахую...», потім воно замінюється
    результатом — так користувач бачить, що бот працює (ТЗ, розділ 27).

    Запит по СПИСКУ країн має інший вигляд відповіді (розклад по країнах +
    унікальний підсумок), тому йде окремим шляхом. `ai_explained` — чи фільтр
    склав ШІ (тоді картка підписує рядок запиту «ШІ зрозумів як»)."""
    if query.is_multi_country:
        await show_multi_country(
            target, services, query, user_id, ai_explained=ai_explained, ai_refine=ai_refine
        )
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
            ai_retry=_wants_ai_retry(
                services, query, ai_explained=ai_explained, ai_refine=ai_refine
            ),
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
    ai_refine: bool = False,
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
    ai_retry = _wants_ai_retry(services, query, ai_explained=ai_explained, ai_refine=ai_refine)
    if query.section_key == "mordy":
        markup = stop_check_menu(ai_retry=ai_retry)
    else:
        # Не-Морди: базове меню — лише «До меню». Кнопку ШІ додаємо через
        # ai_retry_menu (та сама пара: «Уточнити через ШІ» + «До меню»).
        markup = ai_retry_menu() if ai_retry else back_to_menu()
    await status.edit_text(
        render_multi_country(result, suggestions=suggestions, ai_explained=ai_explained),
        reply_markup=markup,
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


def _compute_breakdown(dataset, query: DonorQuery):
    """Синхронна частина розбивки: результат (для шапки/середніх) + повна розбивка
    по країнах (числа). В окремому потоці."""
    return run_query(dataset, query, with_breakdowns=False), country_distribution(dataset, query)


async def show_country_breakdown(
    target: Message | CallbackQuery,
    services: BotServices,
    query: DonorQuery,
    user_id: int,
    *,
    both: bool,
    ai_explained: bool = False,
    ai_refine: bool = False,
) -> None:
    """Розбивка по країнах: топ-N + «Разом» + «…та ще N», з кнопкою «показати всі».

    Для однієї названої бази — повна картка розбивки; коли базу не назвали (both) —
    зведення по обох базах, кожна зі своєю розбивкою й кнопкою «Всі країни: …».
    Назовні йдуть лише кількості по країнах — доменів рушій не віддає (§2.2)."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    status = await message.answer(STATUS_TEXT)
    try:
        if both:
            bases = data_bases(services)
            blocks: list[str] = []
            for key, title in bases:
                dataset = await services.repository.get(key)
                result, dist = await asyncio.to_thread(
                    _compute_breakdown, dataset, query.replace(section_key=key)
                )
                blocks.append(
                    render_country_breakdown_block(
                        title, result, dist, top_n=COUNTRY_BREAKDOWN_TOP_N
                    )
                )
            services.action_log.add(
                user_id, f"розбивка по країнах (обидві бази): {query.describe()}"
            )
            await status.edit_text(
                render_both_bases(query, blocks),
                reply_markup=both_bases_menu(
                    bases,
                    country_breakdown=True,
                    ai_retry=_wants_ai_retry(
                        services, query, ai_explained=ai_explained, ai_refine=ai_refine
                    ),
                ),
            )
            return

        dataset = await services.repository.get(query.section_key)
        result, dist = await asyncio.to_thread(_compute_breakdown, dataset, query)
    except Exception:
        logger.exception("Не вдалося порахувати розбивку по країнах")
        await status.edit_text(
            "⚠️ Не вдалося виконати запит. Спробуйте ще раз або почніть спочатку: /start",
            reply_markup=back_to_menu(),
        )
        return

    services.action_log.add(user_id, f"розбивка по країнах: {render_summary(result)}")
    await status.edit_text(
        render_country_breakdown(
            result, dist, top_n=COUNTRY_BREAKDOWN_TOP_N, ai_explained=ai_explained
        ),
        reply_markup=result_menu(
            query.section_key,
            has_recommendations=False,
            all_countries=True,
            ai_retry=_wants_ai_retry(
                services, query, ai_explained=ai_explained, ai_refine=ai_refine
            ),
        ),
    )


async def show_coverage(
    target: Message | CallbackQuery,
    services: BotServices,
    operation: CoverageQuery,
    user_id: int,
) -> None:
    """Показує покриття за потребою (операція coverage, крок 2).

    ШІ лише розклав запит на цю операцію; рахує детермінований рушій
    (run_coverage), а картка спершу показує, ЯК бот зрозумів запит, і лише потім
    числа. Назовні йдуть тільки кількості по країнах — доменів рушій не віддає."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    status = await message.answer(STATUS_TEXT)
    try:
        dataset = await services.repository.get(operation.section_key)
        result = await asyncio.to_thread(run_coverage, dataset, operation)
    except Exception:
        logger.exception("Не вдалося порахувати покриття за потребою")
        await status.edit_text(
            "⚠️ Не вдалося виконати запит. Спробуйте ще раз або почніть спочатку: /start",
            reply_markup=back_to_menu(),
        )
        return

    # У журнал — лише зведення (скільки країн, скільки закрито), без доменів.
    services.action_log.add(
        user_id,
        f"покриття {result.section_title}: {len(result.covered)}/{len(result.rows)} закрито",
    )
    await status.edit_text(render_coverage(result), reply_markup=back_to_menu())


async def show_comparison(
    target: Message | CallbackQuery,
    services: BotServices,
    operation: ComparisonQuery,
    user_id: int,
) -> None:
    """Рахує кожен варіант окремо тим самим run_query, що й решту запитів."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")
    status = await message.answer(STATUS_TEXT)
    try:
        dataset = await services.repository.get(operation.section_key)
        results = await asyncio.to_thread(
            lambda: tuple(
                run_query(dataset, query, with_breakdowns=False) for query in operation.variants
            )
        )
    except Exception:
        logger.exception("Не вдалося порахувати порівняння")
        await status.edit_text(
            "⚠️ Не вдалося виконати запит. Спробуйте ще раз або почніть спочатку: /start",
            reply_markup=back_to_menu(),
        )
        return
    services.action_log.add(user_id, f"порівняння {dataset.title}: {len(results)} критерії")
    await status.edit_text(render_comparison(operation, results), reply_markup=back_to_menu())


async def resolve_with_ai(services: BotServices, user_id: int, text: str) -> AIOutcome | None:
    """Резервний розбір через ШІ — лише коли ШІ ввімкнено (є ключ).

    Викликається ТІЛЬКИ якщо детермінований словниковий розбір не зрозумів запит.
    Повертає повний AIOutcome (фільтр + причина + НАМІР), щоб викликач розрізнив
    донор-запит, розмовне питання й невдачу. None — коли ШІ вимкнено взагалі.
    ШІ бачить лише текст — не дані."""
    if services.ai is None:
        return None
    return await services.ai.interpret_with_reason(user_id, text)


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

# Вичерпано ліміт викликів ШІ (окремий від загального ліміту бота, ТЗ §11).
AI_LIMIT_TEXT = (
    "🧠 <b>Ліміт запитів до ШІ вичерпано.</b>\n\n"
    "Спробуйте за годину або скористайтеся звичайним запитом чи кнопками меню — /start."
)

# ШІ недоступний: мережа, порожня чи дивна відповідь провайдера.
AI_UNAVAILABLE_TEXT = (
    "🧠 <b>ШІ тимчасово недоступний.</b>\n\n"
    "Спробуйте трохи згодом або скористайтеся звичайним запитом чи кнопками меню — /start."
)

# ШІ відповів, але не у форматі, який бот може застосувати (нерозбірний JSON або
# відповідь обірвалася на ліміті токенів) — кажемо це чесно, не «недоступний».
AI_UNPARSABLE_TEXT = (
    "🧠 <b>Не вдалося розібрати відповідь ШІ.</b>\n\n"
    "ШІ відповів, але не у форматі, який бот може застосувати (можливо, відповідь "
    "була надто довга й обірвалася). Спробуйте переформулювати простіше або "
    "скористайтеся кнопками меню — /start."
)

# ШІ зрозумів текст, але не витягнув із нього жодного дозволеного фільтра.
AI_EMPTY_TEXT = (
    "🧠 <b>ШІ не зрозумів, що саме відфільтрувати.</b>\n\n"
    "Спробуйте вказати країну, мову чи числові пороги явніше або скористайтеся "
    "кнопками меню — /start."
)

AI_STATUS_TEXT = "🧠 Питаю ШІ..."

# Розмовна смуга не змогла відповісти (ліміт квоти ШІ або збій провайдера).
AI_CHAT_FAILED_TEXT = (
    "🧠 <b>Не вдалося відповісти зараз.</b>\n\n"
    "Можливо, вичерпано ліміт запитів до ШІ. Спробуйте трохи згодом або "
    "скористайтеся кнопками меню — /start."
)

# Скільки ОСТАННІХ повідомлень діалогу тримати в контексті консультанта
# (≈3 обміни). Багатоходовість несе всю історію в кожному виклику, тож коротко.
CHAT_HISTORY_MESSAGES = 6


async def reset_chat_history(state) -> None:
    """Скидає контекст консультанта. Кличеться на донор-запиті, щоб консультація
    й підрахунок НЕ змішувались в один контекст (і дешевше по токенах)."""
    if state is not None:
        await state.update_data(chat_history=[])


async def chat_reply(services: BotServices, state, user_id: int, text: str) -> str | None:
    """Відповідь консультанта з контекстом розмови у FSM.

    Історія тримається в межах БЕЗПЕРЕРВНОЇ розмови (донор-запит її скидає через
    reset_chat_history) і обрізається до останніх CHAT_HISTORY_MESSAGES. None —
    коли смуга не змогла (ліміт/збій). Смуга даних не бачить (app/llm/chat.py)."""
    data = await state.get_data() if state is not None else {}
    history = data.get("chat_history") or []
    answer = await services.ai.answer_question(user_id, text, history)
    if answer is None:
        return None
    if state is not None:
        new_history = [
            *history,
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ]
        await state.update_data(chat_history=new_history[-CHAT_HISTORY_MESSAGES:])
    return answer


# Причина невдачі ШІ (AIOutcome.reason) → повідомлення користувачу. Показується
# ЛИШЕ коли й словниковий резерв не зрозумів запит (див. run_ai_query).
_AI_REASON_TEXT = {
    "limit": AI_LIMIT_TEXT,
    "unavailable": AI_UNAVAILABLE_TEXT,
    "unparsable": AI_UNPARSABLE_TEXT,
    "empty": AI_EMPTY_TEXT,
}


async def try_dictionary_query(
    target: Message | CallbackQuery,
    services: BotServices,
    state,
    user_id: int,
    text: str,
    *,
    default_section: str = "magic",
) -> bool:
    """Резервний розбір ТОГО САМОГО тексту СЛОВНИКОМ, коли ШІ не дав фільтра.

    Дзеркалить звичайний вільний текст (freeform.handle_free_text): якщо словник
    розпізнав запит — показує результат (одну базу, обидві бази чи список країн)
    і повертає True. Якщо й словник не зрозумів — НІЧОГО не показує й стан не
    чіпає, повертає False, щоб викликач сам вирішив, який текст невдачі показати.

    Межі безпеки ті самі, що й скрізь: працюємо з агрегатами, донорів не бачимо,
    whitelist полів лишається — це просто інший (детермінований) розбір тексту.
    """
    parsed = parse_free_text(text, default_section=default_section)

    usable = not parsed.needs_clarification
    if usable and not parsed.query.is_multi_country and parsed.query.is_empty:
        # Порожній розбір корисний лише коли явно названо базу й немає нерозпізнаних
        # слів — інакше це не «зрозумілий запит», а мовчазний нуль.
        usable = parsed.section_named and not parsed.unrecognized
    if not usable:
        return False

    # Є що показати: запит стає активним, крок майстра скидаємо.
    await state.set_state(None)
    await state.update_data(**query_to_state(parsed.query, parsed.mentioned))

    # show_both_bases підтримує і звичайний, і multi-country запит. Старий guard
    # `not is_multi_country` тут лишився з версії до підтримки «2 бази × N країн»
    # і ламав саме ШІ-fallback: «Німеччина Канада … Морди і Меджик» ішло лише в
    # першу базу. Намір обох баз не залежить від кількості названих країн.
    show_both = parsed.both_bases or not parsed.section_named
    if show_both:
        await show_both_bases(
            target, services, parsed.query, user_id, explicit_both=parsed.both_bases
        )
    else:
        await show_result(target, services, parsed.query, user_id)
    return True


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

    Якщо ШІ не дав фільтра — спершу пробуємо СЛОВНИК (як звичайний вільний текст),
    і лише коли й він не зрозумів, показуємо повідомлення за причиною невдачі.
    Будь-яка проблема з ШІ → зрозуміле повідомлення, без падіння."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:
        raise RuntimeError("Немає повідомлення, у яке можна відповісти")

    if services.ai is None:
        await message.answer(AI_DISABLED_TEXT, reply_markup=back_to_menu())
        return

    # Не дозволяємо моделі вигадати одну базу з «Меджик або Морди,
    # але не обидві». Це перевіряється до API-виклику: дешевше і надійніше.
    if parse_free_text(text.strip()).ambiguous_bases:
        await message.answer(BASE_CLARIFICATION_TEXT, reply_markup=back_to_menu())
        return

    status = await message.answer(AI_STATUS_TEXT)
    outcome = await services.ai.interpret_with_reason(user_id, text.strip())

    # Гнучка операція (coverage тощо) — має пріоритет. ШІ лише РОЗКЛАВ запит;
    # рахує рушій. Донор-запит скидає контекст консультанта — операція теж.
    if outcome.operation is not None:
        await _delete_or_ignore(status)
        await state.set_state(None)
        await reset_chat_history(state)
        parsed_operation_text = parse_free_text(text.strip())
        logger.info(
            "Маршрут ШІ (користувач %s): операція %s, обидві бази=%s",
            user_id,
            outcome.operation.__class__.__name__,
            parsed_operation_text.both_bases,
        )
        # Операція містить одну технічну section, бо кожен результат рахується
        # на окремому Dataset. Якщо користувач явно назвав обидві бази АБО не
        # назвав жодної, виконуємо ту саму валідовану ШІ-операцію на всіх базах.
        # Фільтр зі словника не беремо: від нього лише детерміновані сигнали баз.
        use_all_bases = parsed_operation_text.both_bases or not parsed_operation_text.section_named
        section_keys = (
            [section_key for section_key, _title in data_bases(services)]
            if use_all_bases
            else [outcome.operation.section_key]
        )
        for section_key in section_keys:
            if isinstance(outcome.operation, ComparisonQuery):
                comparison = replace(
                    outcome.operation,
                    section_key=section_key,
                    variants=tuple(
                        replace(query, section_key=section_key)
                        for query in outcome.operation.variants
                    ),
                )
                await show_comparison(message, services, comparison, user_id)
            else:
                await show_coverage(
                    message,
                    services,
                    replace(outcome.operation, section_key=section_key),
                    user_id,
                )
        return

    if outcome.query is None:
        await _delete_or_ignore(status)

        # Розмовне питання («що таке заспамленість?», «як користуватись ботом?»)
        # → окрема РОЗМОВНА смуга (без доступу до даних, chat.py). Лише коли модель
        # прямо сказала intent=question; на збої/ліміті intent лишається filter.
        if outcome.intent == "question":
            logger.info("Маршрут ШІ (користувач %s): розмовна смуга", user_id)
            answer = await chat_reply(services, state, user_id, text.strip())
            await message.answer(
                f"🧠 {escape(answer)}" if answer else AI_CHAT_FAILED_TEXT,
                reply_markup=back_to_menu(),
            )
            return

        # Не питання → пробуємо СЛОВНИК на тому самому тексті (Фаза 2A). Глухий кут
        # лишається, ЛИШЕ коли НІ ШІ, НІ словник не зрозуміли запит.
        data = await state.get_data()
        if await try_dictionary_query(
            message,
            services,
            state,
            user_id,
            text.strip(),
            default_section=data.get("section_key", "magic"),
        ):
            logger.info("Маршрут ШІ (користувач %s): словниковий фолбек", user_id)
            return
        # Валідна, але порожня відповідь означає, що запит може бути
        # нестандартним або неповним. Замість глухого кута даємо консультанту
        # пояснити обмеження або поставити 1–2 уточнювальні питання. Оригінал
        # зберігаємо: наступна репліка користувача буде додана до нього і знову
        # пройде валідований ШІ-інтерпретатор.
        if outcome.reason == "empty":
            answer = await chat_reply(services, state, user_id, text.strip())
            if answer:
                await state.update_data(ai_pending_text=text.strip())
                await state.set_state(Ask.ai_query)
                await message.answer(
                    f"🧠 {escape(answer)}\n\n<i>Відповідайте на уточнення тут же.</i>",
                    reply_markup=back_to_menu(),
                )
                logger.info("Маршрут ШІ (користувач %s): запит на уточнення", user_id)
                return
        # На мережевому збої, ліміті чи нерозбірному JSON не робимо другий
        # виклик того самого несправного API; показуємо точну причину.
        await message.answer(
            _AI_REASON_TEXT.get(outcome.reason, AI_FAILED_TEXT),
            reply_markup=back_to_menu(),
        )
        return

    logger.info("Маршрут ШІ (користувач %s): донор-запит", user_id)
    query = outcome.query

    # Розпізнане ШІ стає активним запитом (як звичайний), стан скидаємо.
    # Донор-запит скидає контекст консультанта: розмова й підрахунок не змішуються.
    await state.set_state(None)
    await state.update_data(**query_to_state(query), ai_pending_text="")
    await reset_chat_history(state)
    # Прибираємо статус «Питаю ШІ...» і показуємо картку окремим повідомленням.
    await _delete_or_ignore(status)

    # Контракт ШІ однобазовий, тож рішення «одна база чи всі» беремо з
    # оригінального тексту: явна одна база → одна; «обидві» або жодної → всі.
    # Сам фільтр лишається валідованим ШІ-інтерпретатором.
    #
    # Пункт III: show_both_bases тепер уміє й список країн (рахує run_multi_country
    # на кожну базу), тож guard на multi-country знято — «меджик і морди британія
    # і німеччина» через ШІ дає обидві бази × обидві країни.
    parsed = parse_free_text(text.strip())
    # Розбивка по країнах, якщо в тексті є слово-сигнал («які країни», «по країнах»).
    if parsed.wants_country_breakdown and not query.is_multi_country:
        await show_country_breakdown(
            message,
            services,
            query,
            user_id,
            both=parsed.both_bases or not parsed.section_named,
            ai_explained=True,
        )
        return
    if parsed.both_bases or not parsed.section_named:
        await show_both_bases(
            message,
            services,
            query,
            user_id,
            explicit_both=parsed.both_bases,
            ai_explained=True,
        )
        return
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
