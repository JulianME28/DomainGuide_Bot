"""Майстер-запит: покрокове складання запиту кнопками.

Порядок кроків: база → КРАЇНА → трафік → DR → резюме → запуск.

Крок називається саме «КРАЇНА», а не «гео». Це не дрібниця: колонки гео в
даних немає, країна визначається за доменною зоною. Слово «гео» обіцяло б
те, чого бот не вміє.

На кожному кроці працюють обидва способи: натиснути кнопку або просто
написати текстом. Якщо на кроці трафіку написати «250», бот це зрозуміє —
окремо натискати «Ввести вручну» не обов'язково.

Кнопки «Назад», «Скинути» і «До меню» є на кожному кроці (ТЗ, розділ 29).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.analytics.query import DIMENSION_ACCUSATIVE, Dimension
from app.bot.context import BotServices
from app.bot.execution import safe_edit, show_result
from app.bot.keyboards import (
    cancel_only,
    main_menu,
    wizard_confirm,
    wizard_countries,
    wizard_dr,
    wizard_sections,
    wizard_traffic,
)
from app.bot.states import (
    FRESH_KEY,
    Wizard,
    fresh_from_state,
    inherited_dimensions,
    query_from_state,
    query_to_state,
    summary_lines,
)
from app.data.parsing import parse_number
from app.dictionary.countries import country_by_code
from app.dictionary.resolver import find_country_match, resolve_language, scan_entities

router = Router(name="wizard")

STEP_COUNTRY = (
    "🌍 <b>Крок 1 з 3. Оберіть КРАЇНУ</b>\n\n"
    "Можна натиснути кнопку, написати назву (<code>Німеччина</code>, "
    "<code>Germany</code>) або доменну зону (<code>.de</code>).\n\n"
    "<i>Країна визначається за доменною зоною. Мовний зріз бот додасть "
    "окремим рядком у відповіді.</i>"
)

STEP_TRAFFIC = "📊 <b>Крок 2 з 3. Фільтр по трафіку</b>\n\nОберіть варіант або напишіть число."

STEP_DR = "📈 <b>Крок 3 з 3. Фільтр по DR</b>\n\nОберіть варіант або напишіть число."


async def _show(target: CallbackQuery | Message, text: str, markup: InlineKeyboardMarkup) -> None:
    """Показує крок — байдуже, прийшли ми з кнопки чи з тексту."""
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _goto_country(target: CallbackQuery | Message, state: FSMContext) -> None:
    await state.set_state(Wizard.country)
    await _show(target, STEP_COUNTRY, wizard_countries())


async def _goto_traffic(target: CallbackQuery | Message, state: FSMContext) -> None:
    await state.set_state(Wizard.traffic)
    await _show(target, STEP_TRAFFIC, wizard_traffic())


async def _goto_dr(target: CallbackQuery | Message, state: FSMContext) -> None:
    await state.set_state(Wizard.dr)
    await _show(target, STEP_DR, wizard_dr())


async def _mark_fresh(state: FSMContext, dimension: str) -> None:
    """Позначає вимір як заданий САМЕ ЗАРАЗ.

    Далі резюме за цією позначкою відрізняє щойно обране від того, що
    лишилося з попереднього запиту.
    """
    data = await state.get_data()
    fresh = set(data.get(FRESH_KEY) or ())
    fresh.add(dimension)
    await state.update_data(**{FRESH_KEY: sorted(fresh)})


async def _goto_confirm(
    target: CallbackQuery | Message, services: BotServices, state: FSMContext
) -> None:
    """Резюме фільтрів перед запуском (ТЗ, розділ 30)."""
    await state.set_state(Wizard.confirm)

    data = await state.get_data()
    query = query_from_state(data)
    fresh = fresh_from_state(data)

    text = summary_lines(query, services.section_title(query.section_key), fresh)
    # Кнопки скидання показуємо лише для успадкованих фільтрів: щойно
    # обране змінюють кнопкою «Назад», а не «Прибрати».
    await _show(target, text, wizard_confirm(inherited_dimensions(query, fresh)))


# ---------------------------------------------------------------------------
# Старт і навігація
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("wizard:start"))
async def start_wizard(callback: CallbackQuery, state: FSMContext) -> None:
    """Запуск майстра. Якщо базу вже обрано — одразу до країни.

    Фільтри з попереднього запиту НЕ стираються (ТЗ, розділ 29 — бот
    пам'ятає запит до скидання), але всі вони одразу позначаються як
    успадковані. Тому в резюме буде видно, що прийшло з минулого разу,
    а що обрано щойно.
    """
    parts = callback.data.split(":")
    section_key = parts[2] if len(parts) > 2 else None

    await state.update_data(**{FRESH_KEY: []})

    if section_key:
        await state.update_data(section_key=section_key)
        await _goto_country(callback, state)
        return

    await state.set_state(Wizard.country)
    await _show(callback, "🧙 <b>Майстер-запит</b>\n\nОберіть базу:", wizard_sections())


@router.callback_query(F.data.startswith("wizard:section:"))
async def choose_section(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(section_key=callback.data.split(":")[2])
    await _goto_country(callback, state)


@router.callback_query(F.data == "wizard:reset")
async def reset_wizard(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """Скидає весь запит (ТЗ, розділ 29)."""
    await state.clear()
    is_admin = services.settings.is_admin(callback.from_user.id)
    await safe_edit(
        callback,
        "🔄 Запит скинуто.\n\n<b>Оберіть базу:</b>",
        main_menu(is_admin=is_admin),
    )
    await callback.answer("Скинуто")


@router.callback_query(F.data.startswith("wizard:drop:"))
async def drop_dimension(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """Прибирає ОДИН успадкований фільтр, не чіпаючи решту запиту.

    Це і є «один дотик на скидання»: зайва мова з минулого запиту
    прибирається однією кнопкою, а обрана щойно країна лишається.
    """
    dimension = callback.data.split(":")[2]

    query = query_from_state(await state.get_data()).without(dimension)
    fresh = fresh_from_state(await state.get_data()) - {dimension}

    await state.update_data(**query_to_state(query, fresh))

    title = DIMENSION_ACCUSATIVE.get(dimension, dimension)
    await callback.answer(f"Прибрано: {title}")
    await _goto_confirm(callback, services, state)


@router.callback_query(F.data.startswith("wizard:back:"))
async def go_back(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """Крок назад."""
    step = callback.data.split(":")[2]
    if step == "country":
        await _goto_country(callback, state)
    elif step == "traffic":
        await _goto_traffic(callback, state)
    elif step == "dr":
        await _goto_dr(callback, state)
    else:
        await _goto_confirm(callback, services, state)


# ---------------------------------------------------------------------------
# Крок «КРАЇНА»
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("wizard:country:"))
async def pick_country(callback: CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[2]

    if choice == "manual":
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "✍️ Напишіть країну: <code>Португалія</code>, <code>Japan</code> "
                "або <code>.jp</code>",
                reply_markup=cancel_only(),
            )
        return

    if choice == "skip":
        # «Пропустити» = свідомо без країни. Успадковану теж прибираємо:
        # інакше вийшло б, що людина пропустила крок, а фільтр лишився.
        await state.update_data(country_code=None, zones=[])
        await _mark_fresh(state, Dimension.COUNTRY)
        await _goto_traffic(callback, state)
        return

    country = country_by_code(choice)
    if country is None:
        await callback.answer("Невідома країна", show_alert=True)
        return

    await state.update_data(country_code=country.code, zones=[])
    await _mark_fresh(state, Dimension.COUNTRY)
    await _goto_traffic(callback, state)


@router.message(Wizard.country)
async def type_country(message: Message, state: FSMContext) -> None:
    """Країна, написана текстом просто на кроці вибору."""
    text = message.text or ""

    # Спершу перевіряємо, чи це не мова: «англійською» — не країна.
    entities = scan_entities(text)
    if entities.country is None and entities.language is not None:
        await state.update_data(language_code=entities.language.code)
        await _mark_fresh(state, Dimension.LANGUAGE)
        await message.answer(
            f"Це мова, а не країна — записав її як фільтр мови "
            f"({entities.language.name_uk}).\n"
            "Країну можна обрати кнопкою або пропустити."
        )
        await _goto_traffic(message, state)
        return

    found = find_country_match(text, allow_short=True)
    if found is None:
        await message.answer(
            "Не впізнав країну. Спробуйте кнопку або напишіть інакше: "
            "<code>Німеччина</code>, <code>Germany</code>, <code>.de</code>.",
            reply_markup=wizard_countries(),
        )
        return

    await state.update_data(country_code=found[0].code, zones=[])
    await _mark_fresh(state, Dimension.COUNTRY)
    await _goto_traffic(message, state)


# ---------------------------------------------------------------------------
# Крок «Трафік»
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("wizard:traffic:"))
async def pick_traffic(callback: CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[2]

    if choice == "manual":
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "✍️ Напишіть мінімальний трафік числом, наприклад <code>250</code>",
                reply_markup=cancel_only(),
            )
        return

    is_any = choice == "any"
    await state.update_data(
        traffic_min=None if is_any else float(choice),
        # «Не важливо» знімає і верхню межу, якщо вона лишилася з минулого запиту.
        traffic_max=None,
    )
    await _mark_fresh(state, Dimension.TRAFFIC)
    await _goto_dr(callback, state)


@router.message(Wizard.traffic)
async def type_traffic(message: Message, state: FSMContext) -> None:
    value = parse_number(message.text)
    if value is None:
        await message.answer(
            "Потрібне число, наприклад <code>100</code>. Або натисніть «Не важливо».",
            reply_markup=wizard_traffic(),
        )
        return

    await state.update_data(traffic_min=value, traffic_max=None)
    await _mark_fresh(state, Dimension.TRAFFIC)
    await _goto_dr(message, state)


# ---------------------------------------------------------------------------
# Крок «DR»
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("wizard:dr:"))
async def pick_dr(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    choice = callback.data.split(":")[2]

    if choice == "manual":
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "✍️ Напишіть мінімальний DR числом, наприклад <code>35</code>",
                reply_markup=cancel_only(),
            )
        return

    await state.update_data(dr_min=None if choice == "any" else float(choice), dr_max=None)
    await _mark_fresh(state, Dimension.DR)
    await _goto_confirm(callback, services, state)


@router.message(Wizard.dr)
async def type_dr(message: Message, services: BotServices, state: FSMContext) -> None:
    value = parse_number(message.text)
    if value is None:
        await message.answer(
            "Потрібне число, наприклад <code>30</code>. Або натисніть «Не важливо».",
            reply_markup=wizard_dr(),
        )
        return

    await state.update_data(dr_min=value, dr_max=None)
    await _mark_fresh(state, Dimension.DR)
    await _goto_confirm(message, services, state)


# ---------------------------------------------------------------------------
# Додаткова мова і запуск
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "wizard:addlang")
async def ask_language(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Wizard.language)
    await safe_edit(
        callback,
        "🗣 <b>Додати фільтр по мові</b>\n\n"
        "Напишіть мову: <code>німецькою</code>, <code>German</code>, <code>англомовні</code>.\n\n"
        "<i>Разом із країною це дасть перетин: донори в зоні країни І цією мовою.</i>",
        cancel_only(),
    )
    await callback.answer()


@router.message(Wizard.language)
async def type_language(message: Message, services: BotServices, state: FSMContext) -> None:
    language = resolve_language(message.text or "", allow_short=True)
    if language is None:
        await message.answer(
            "Не впізнав мову. Спробуйте: <code>німецькою</code>, <code>German</code>.",
            reply_markup=cancel_only(),
        )
        return

    await state.update_data(language_code=language.code)
    await _mark_fresh(state, Dimension.LANGUAGE)
    await _goto_confirm(message, services, state)


@router.callback_query(F.data == "wizard:run")
async def run_wizard(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """Запускає перевірку за зібраними фільтрами."""
    query = query_from_state(await state.get_data())

    # Запит лишається в пам'яті — щоб працювали кнопки під результатом
    # і щоб користувач міг додати фільтр, не збираючи все заново.
    await state.set_state(None)
    await state.update_data(**query_to_state(query))

    await callback.answer()
    await show_result(callback, services, query, callback.from_user.id)
