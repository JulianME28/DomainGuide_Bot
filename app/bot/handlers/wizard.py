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
    wizard_geo,
    wizard_sections,
    wizard_spam,
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
    "🌍 <b>Крок 1. Оберіть КРАЇНУ</b>\n\n"
    "Можна натиснути кнопку, написати назву (<code>Німеччина</code>, "
    "<code>Germany</code>) або доменну зону (<code>.de</code>).\n\n"
    "<i>Країна визначається за доменною зоною. Мовний зріз бот додасть "
    "окремим рядком у відповіді.</i>"
)

STEP_GEO = (
    "🌐 <b>Гео (країна трафіку)</b>\n\n"
    "Фільтр по колонці GEO — країна, ЗВІДКИ йде трафік (не доменна зона). "
    "Напишіть країну (<code>Польща</code>, <code>Poland</code>) або натисніть "
    "«Не важливо».\n\n"
    "<i>Це окремо від кроку «Країна»: там — доменна зона, тут — походження трафіку.</i>"
)

STEP_TRAFFIC = "📊 <b>Фільтр по трафіку</b>\n\nОберіть варіант або напишіть число."

STEP_DR = "📈 <b>Фільтр по DR</b>\n\nОберіть варіант або напишіть число."

STEP_SPAM = (
    "🧪 <b>Фільтр по заспамленості</b>\n\n"
    "У <b>кількості</b> заспамлених лінків (не у відсотках), менше = краще. "
    "Слово «вихідні» теж рахується сюди — це заспамленість, а не окремий фільтр.\n"
    "Оберіть варіант або напишіть число — воно означатиме «до N»."
)


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


async def _goto_geo(target: CallbackQuery | Message, state: FSMContext) -> None:
    await state.set_state(Wizard.geo)
    await _show(target, STEP_GEO, wizard_geo())


async def _goto_traffic(
    target: CallbackQuery | Message, state: FSMContext, *, back: str = "country"
) -> None:
    await state.set_state(Wizard.traffic)
    await _show(target, STEP_TRAFFIC, wizard_traffic(back=back))


async def _after_country(
    target: CallbackQuery | Message, services: BotServices, state: FSMContext
) -> None:
    """Після країни: для баз із колонкою GEO — крок гео, інакше одразу трафік."""
    section = await _section_of(services, state)
    if section.has_geo:
        await _goto_geo(target, state)
    else:
        await _goto_traffic(target, state, back="country")


def _traffic_back(section) -> str:
    """Куди веде «Назад» із кроку трафіку: на гео (якщо є) чи на країну."""
    return "geo" if section.has_geo else "country"


async def _goto_dr(target: CallbackQuery | Message, state: FSMContext) -> None:
    await state.set_state(Wizard.dr)
    await _show(target, STEP_DR, wizard_dr())


async def _goto_spam(target: CallbackQuery | Message, state: FSMContext) -> None:
    await state.set_state(Wizard.spam)
    await _show(target, STEP_SPAM, wizard_spam())


async def _section_of(services: BotServices, state: FSMContext):
    """Налаштування бази поточного запиту — щоб знати, чи має вона спам/вихідні."""
    data = await state.get_data()
    return services.columns.section(data.get("section_key", "magic"))


async def _after_dr(
    target: CallbackQuery | Message, services: BotServices, state: FSMContext
) -> None:
    """Після DR: для «Морд» — крок заспамленості, інакше одразу резюме.

    Саме тут ховається крок заспамленості для «Меджика»: колонки немає —
    майстер його просто не показує й веде одразу до резюме. Окремого кроку
    «вихідні» немає: стовпець F числом не фільтрується.
    """
    section = await _section_of(services, state)
    if section.tracks_spam:
        await _goto_spam(target, state)
    else:
        await _goto_confirm(target, services, state)


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

    section = services.columns.section(query.section_key)
    text = summary_lines(
        query,
        services.section_title(query.section_key),
        fresh,
        tracks_spam=section.tracks_spam,
        tracks_geo=section.has_geo,
    )
    # «Назад» веде на останній крок перед резюме: для «Морд» це заспамленість,
    # для «Меджика» — DR (кроку спаму там немає).
    back = "spam" if section.tracks_spam else "dr"
    # Кнопки скидання показуємо лише для успадкованих фільтрів: щойно
    # обране змінюють кнопкою «Назад», а не «Прибрати».
    await _show(target, text, wizard_confirm(inherited_dimensions(query, fresh), back=back))


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
    elif step == "geo":
        await _goto_geo(callback, state)
    elif step == "traffic":
        await _goto_traffic(callback, state, back=_traffic_back(await _section_of(services, state)))
    elif step == "dr":
        await _goto_dr(callback, state)
    elif step == "spam":
        await _goto_spam(callback, state)
    else:
        await _goto_confirm(callback, services, state)


# ---------------------------------------------------------------------------
# Крок «КРАЇНА»
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("wizard:country:"))
async def pick_country(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
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
        await _after_country(callback, services, state)
        return

    country = country_by_code(choice)
    if country is None:
        await callback.answer("Невідома країна", show_alert=True)
        return

    await state.update_data(country_code=country.code, zones=[])
    await _mark_fresh(state, Dimension.COUNTRY)
    await _after_country(callback, services, state)


@router.message(Wizard.country)
async def type_country(message: Message, services: BotServices, state: FSMContext) -> None:
    """Країна, написана текстом просто на кроці вибору."""
    text = message.text or ""

    # Спершу перевіряємо, чи це не мова: «англійською» — не країна.
    entities = scan_entities(text)
    if entities.country is None and entities.language is not None:
        await state.update_data(
            language_code=entities.language.code,
            language_codes=[entities.language.code],
        )
        await _mark_fresh(state, Dimension.LANGUAGE)
        await message.answer(
            f"Це мова, а не країна — записав її як фільтр мови "
            f"({entities.language.name_uk}).\n"
            "Країну можна обрати кнопкою або пропустити."
        )
        await _after_country(message, services, state)
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
    await _after_country(message, services, state)


# ---------------------------------------------------------------------------
# Крок «ГЕО (країна трафіку)» — фільтр по колонці GEO, для баз із цією колонкою
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "wizard:geo:any")
async def skip_geo(callback: CallbackQuery, state: FSMContext) -> None:
    """«Не важливо» на кроці гео — знімаємо GEO-фільтр і йдемо до трафіку."""
    await state.update_data(geo_code=None)
    await _mark_fresh(state, Dimension.GEO)
    await _goto_traffic(callback, state, back="geo")


@router.message(Wizard.geo)
async def type_geo(message: Message, state: FSMContext) -> None:
    """Країна ПОХОДЖЕННЯ ТРАФІКУ, написана текстом на кроці гео."""
    found = find_country_match(message.text or "", allow_short=True)
    if found is None:
        await message.answer(
            "Не впізнав країну. Напишіть інакше: <code>Польща</code>, "
            "<code>Poland</code>, або натисніть «Не важливо».",
            reply_markup=wizard_geo(),
        )
        return

    await state.update_data(geo_code=found[0].code)
    await _mark_fresh(state, Dimension.GEO)
    await _goto_traffic(message, state, back="geo")


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
    await _after_dr(callback, services, state)


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
    await _after_dr(message, services, state)


# ---------------------------------------------------------------------------
# Крок «Заспамленість» (стовпець G — кількість заспамлених лінків, «Морди»)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("wizard:spam:"))
async def pick_spam(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    choice = callback.data.split(":")[2]

    if choice == "manual":
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "✍️ Напишіть максимальну к-сть заспамлених лінків (до N), напр. <code>20</code>",
                reply_markup=cancel_only(),
            )
        return

    is_any = choice == "any"
    await state.update_data(
        spam_min=None,
        spam_max=None if is_any else float(choice),
    )
    await _mark_fresh(state, Dimension.SPAM)
    await _goto_confirm(callback, services, state)


@router.message(Wizard.spam)
async def type_spam(message: Message, services: BotServices, state: FSMContext) -> None:
    value = parse_number(message.text)
    if value is None:
        await message.answer(
            "Потрібне число, наприклад <code>20</code>. Або натисніть «Не важливо».",
            reply_markup=wizard_spam(),
        )
        return

    await state.update_data(spam_min=None, spam_max=value)
    await _mark_fresh(state, Dimension.SPAM)
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

    await state.update_data(language_code=language.code, language_codes=[language.code])
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
