"""Меню баз і швидкі запити: по країні, по мові, по доменній зоні.

Тут же живуть кнопки під карткою результату — «уточнити гео», «розподіл
по мовах», «підібрати близькі донори».
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.analytics.query import DonorQuery
from app.bot.context import BotServices
from app.bot.execution import (
    execute,
    resolve_with_ai,
    safe_edit,
    show_both_bases,
    show_result,
)
from app.bot.keyboards import (
    back_to_menu,
    cancel_only,
    country_picker,
    cross_mode_keyboard,
    result_menu,
    section_menu,
)
from app.bot.states import Ask, query_from_state, query_to_state
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from app.dictionary.resolver import (
    find_country_match,
    hint_for_country_mode,
    hint_for_language_mode,
    resolve_language,
)
from app.text.cards import render_breakdown, render_recommendations
from app.text.freeform import parse_free_text
from app.text.prompts import cross_mode_prompt

router = Router(name="sections")

SUBMITS_TEXT = (
    "📩 <b>Сабміти</b>\n\n"
    "Розділ додано в структуру бота, але до даних він поки не підключений — "
    "цей аркуш не читається.\n\n"
    "Щоб увімкнути його пізніше, треба вказати аркуш і колонки у файлі "
    "<code>config/columns.toml</code>. Код бота міняти не доведеться."
)


async def _open_section(target: CallbackQuery | Message, services: BotServices, key: str) -> None:
    """Показує меню розділу або пояснення для заглушки."""
    section = services.columns.section(key)

    if not section.reads_data:
        text, markup = SUBMITS_TEXT, back_to_menu()
    else:
        text = f"🗂 <b>База: {section.title}</b>\n\nЩо потрібно перевірити?"
        markup = section_menu(key)

    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("menu:section:"))
async def open_section(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    await state.set_state(None)
    await _open_section(callback, services, callback.data.split(":")[2])


@router.message(Command("magic"))
async def cmd_magic(message: Message, services: BotServices) -> None:
    await _open_section(message, services, "magic")


@router.message(Command("mordy"))
async def cmd_mordy(message: Message, services: BotServices) -> None:
    await _open_section(message, services, "mordy")


@router.message(Command("submits"))
async def cmd_submits(message: Message, services: BotServices) -> None:
    await _open_section(message, services, "submits")


# ---------------------------------------------------------------------------
# Швидкий запит по країні
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("pick:country:"))
async def pick_country(callback: CallbackQuery) -> None:
    section_key = callback.data.split(":")[2]
    await safe_edit(
        callback,
        "🌍 <b>Оберіть країну</b>\n\n"
        "<i>Країна визначається за доменною зоною. Мовний зріз бот покаже "
        "окремим рядком у відповіді.</i>",
        country_picker(section_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("q:country:"))
async def query_country(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    _, _, section_key, code = callback.data.split(":")
    country = country_by_code(code)
    if country is None:
        await callback.answer("Невідома країна", show_alert=True)
        return

    query = DonorQuery(section_key=section_key, country=country)
    # Кнопку могли натиснути з режиму введення (напр. підказки) — знімаємо стан,
    # щоб наступний текст не потрапив у старий крок.
    await state.set_state(None)
    await state.update_data(**query_to_state(query))
    await callback.answer()
    await show_result(callback, services, query, callback.from_user.id)


@router.callback_query(F.data.startswith("q:lang:"))
async def query_language(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """Швидкий запит по мові — дзеркало q:country. Потрібен для кнопок підказки."""
    _, _, section_key, code = callback.data.split(":")
    language = language_by_code(code)
    if language is None:
        await callback.answer("Невідома мова", show_alert=True)
        return

    query = DonorQuery(section_key=section_key, language=language)
    await state.set_state(None)
    await state.update_data(**query_to_state(query))
    await callback.answer()
    await show_result(callback, services, query, callback.from_user.id)


# ---------------------------------------------------------------------------
# Запит по мові, зоні або країні, введеній вручну
# ---------------------------------------------------------------------------

_ASK_PROMPTS = {
    "country": (
        Ask.country,
        "✍️ Напишіть країну: <code>Німеччина</code>, <code>Britain</code> або <code>.de</code>",
    ),
    "language": (
        Ask.language,
        "🗣 Напишіть мову: <code>німецькою</code>, <code>German</code>, <code>англомовні</code>",
    ),
    "zone": (
        Ask.zone,
        "🔗 Напишіть доменну зону з крапкою:\n"
        "<code>.de</code>, <code>.co.uk</code>, <code>.com</code>",
    ),
    "free": (
        Ask.free_text,
        "🤖 Опишіть запит своїми словами. Наприклад:\n"
        "<code>Британія, трафік від 1, DR не важливий</code>",
    ),
}


@router.callback_query(F.data.startswith("ask:"))
async def ask_for_input(callback: CallbackQuery, state: FSMContext) -> None:
    _, kind, section_key = callback.data.split(":")
    target_state, prompt = _ASK_PROMPTS[kind]

    await state.update_data(section_key=section_key)
    await state.set_state(target_state)
    await safe_edit(callback, prompt, cancel_only())
    await callback.answer()


@router.message(Ask.country)
async def receive_country(message: Message, services: BotServices, state: FSMContext) -> None:
    """Приймає назву країни, введену вручну."""
    data = await state.get_data()
    section_key = data.get("section_key", "magic")
    text = message.text or ""

    # Спершу дивимось, чи це раптом не мова: «українською» — не країна.
    # Якщо так — не рахуємо мовчки, а показуємо дзеркальну підказку з вибором.
    hint = hint_for_country_mode(text)
    if hint is not None:
        await message.answer(
            cross_mode_prompt(hint, mode="country"),
            reply_markup=cross_mode_keyboard(section_key, hint, mode="country"),
        )
        return

    found = find_country_match(text, allow_short=True)
    if found is None:
        await message.answer(
            "Не впізнав країну. Спробуйте інакше: <code>Німеччина</code>, "
            "<code>Germany</code> або <code>.de</code>.",
            reply_markup=cancel_only(),
        )
        return

    query = DonorQuery(section_key=section_key, country=found[0])
    await state.set_state(None)
    await state.update_data(**query_to_state(query))
    await show_result(message, services, query, message.from_user.id)


@router.message(Ask.language)
async def receive_language(message: Message, services: BotServices, state: FSMContext) -> None:
    """Приймає назву мови, введену вручну."""
    data = await state.get_data()
    section_key = data.get("section_key", "magic")
    text = message.text or ""

    language = resolve_language(text, allow_short=True)
    if language is not None:
        query = DonorQuery(section_key=section_key, language=language)
        await state.set_state(None)
        await state.update_data(**query_to_state(query))
        await show_result(message, services, query, message.from_user.id)
        return

    # Не мова. Може, це доменна зона чи країна («.ua», «Німеччина»)? Тоді не
    # віддаємо порожній результат, а пояснюємо і пропонуємо два варіанти.
    hint = hint_for_language_mode(text)
    if hint is not None:
        await message.answer(
            cross_mode_prompt(hint, mode="language"),
            reply_markup=cross_mode_keyboard(section_key, hint, mode="language"),
        )
        return

    await message.answer(
        "Не впізнав мову. Спробуйте: <code>німецькою</code>, <code>German</code>, "
        "<code>англомовні</code>.",
        reply_markup=cancel_only(),
    )


@router.message(Ask.zone)
async def receive_zone(message: Message, services: BotServices, state: FSMContext) -> None:
    """Приймає доменну зону, введену вручну."""
    from app.dictionary.normalize import find_zone_mentions, normalize_text
    from app.dictionary.zones import is_global_zone

    data = await state.get_data()
    section_key = data.get("section_key", "magic")

    mentions = find_zone_mentions(normalize_text(message.text or ""))
    if not mentions:
        await message.answer(
            "Зону треба писати з крапкою: <code>.de</code>, <code>.co.uk</code>, "
            "<code>.com</code>.",
            reply_markup=cancel_only(),
        )
        return

    zone = mentions[0][0]
    country = None if is_global_zone(zone) else country_by_code(_country_code_for(zone))

    # Для зони країни рахуємо як запит про країну — тоді буде й мовний рядок.
    query = (
        DonorQuery(section_key=section_key, country=country)
        if country
        else DonorQuery(section_key=section_key, zones=(zone,))
    )

    await state.set_state(None)
    await state.update_data(**query_to_state(query))
    await show_result(message, services, query, message.from_user.id)


def _country_code_for(zone: str) -> str:
    from app.dictionary.countries import country_by_zone

    country = country_by_zone(zone)
    return country.code if country else ""


@router.message(Ask.free_text)
async def receive_free_text(message: Message, services: BotServices, state: FSMContext) -> None:
    """Вільний запит, коли його попросили кнопкою."""
    data = await state.get_data()
    text = message.text or ""
    parsed = parse_free_text(text, default_section=data.get("section_key", "magic"))

    if parsed.needs_clarification:
        # Словник не зрозумів — резервно пробуємо ШІ (якщо ввімкнено).
        ai_query = await resolve_with_ai(services, message.from_user.id, text)
        if ai_query is not None:
            await state.set_state(None)
            await state.update_data(**query_to_state(ai_query))
            await show_result(message, services, ai_query, message.from_user.id)
            return

        if parsed.unrecognized:
            from app.text.cards import render_not_understood

            await message.answer(
                render_not_understood(parsed.unrecognized), reply_markup=cancel_only()
            )
            return

        from app.text.freeform import CLARIFICATION_TEXT

        await message.answer(CLARIFICATION_TEXT, reply_markup=cancel_only())
        return

    await state.set_state(None)
    await state.update_data(**query_to_state(parsed.query, parsed.mentioned))

    # Порожній запит без валідного фільтра — базу НЕ вивалюємо.
    if not parsed.query.is_multi_country and parsed.query.is_empty:
        if parsed.unrecognized:
            from app.text.cards import render_not_understood

            await message.answer(
                render_not_understood(parsed.unrecognized), reply_markup=cancel_only()
            )
            return
        if not parsed.section_named:
            from app.text.freeform import EMPTY_QUERY_HINT

            await message.answer(EMPTY_QUERY_HINT, reply_markup=cancel_only())
            return

    # Зведено по обох базах, коли базу не назвали явно АБО назвали переліком
    # («(Меджик + Морди)», «в обох базах»). Список країн має власний вигляд.
    # Підсумок (унікальних донорів разом) зведення показує завжди, угорі.
    show_both = not parsed.query.is_multi_country and (
        parsed.both_bases or not parsed.section_named
    )
    if show_both:
        await show_both_bases(
            message,
            services,
            parsed.query,
            message.from_user.id,
            explicit_both=parsed.both_bases,
        )
        return
    await show_result(message, services, parsed.query, message.from_user.id)


# ---------------------------------------------------------------------------
# Кнопки під карткою результату
# ---------------------------------------------------------------------------


async def _current_query(state: FSMContext) -> DonorQuery | None:
    data = await state.get_data()
    return query_from_state(data) if data.get("section_key") else None


@router.callback_query(F.data.startswith("res:detail:"))
async def show_base_detail(
    callback: CallbackQuery, services: BotServices, state: FSMContext
) -> None:
    """«Детально по …» під зведеним показом — повна картка вибраної бази.

    Той самий запит зі збереженими фільтрами, лише виконаний в одній базі."""
    section_key = callback.data.split(":")[2]
    query = await _current_query(state)
    if query is None:
        await callback.answer("Спочатку зробіть запит", show_alert=True)
        return

    detailed = query.replace(section_key=section_key)
    await state.update_data(**query_to_state(detailed))
    await callback.answer()
    await show_result(callback, services, detailed, callback.from_user.id)


@router.callback_query(F.data == "res:geo")
async def show_geo_breakdown(
    callback: CallbackQuery, services: BotServices, state: FSMContext
) -> None:
    """«Уточнити гео в цій групі» (ТЗ, розділ 13.4)."""
    query = await _current_query(state)
    if query is None:
        await callback.answer("Спочатку зробіть запит", show_alert=True)
        return

    executed = await execute(services, query)
    text = render_breakdown("🌍 Гео всередині цієї групи", executed.result.country_breakdown)
    text += (
        "\n\n<i>Гео визначається за доменною зоною. Глобальні зони "
        "(.com, .net, .org) не належать жодній країні, тому показані окремо.</i>"
    )

    if callback.message:
        await callback.message.answer(text, reply_markup=result_menu(query.section_key))
    await callback.answer()


@router.callback_query(F.data.startswith("res:runin:"))
async def run_in_base(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """«Виконати цей запит у базі X» — той самий запит в іншій базі.

    Потрібно, коли фільтр (напр. заспамленість) відкинуто, бо поточна база не
    має таких колонок, а інша — має."""
    section_key = callback.data.split(":")[2]
    query = await _current_query(state)
    if query is None:
        await callback.answer("Спочатку зробіть запит", show_alert=True)
        return

    new_query = query.replace(section_key=section_key)
    await state.update_data(**query_to_state(new_query))
    await callback.answer()
    await show_result(callback, services, new_query, callback.from_user.id)


@router.callback_query(F.data == "res:zoneonly")
async def show_zone_only(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """«Тільки доменна зона» — той самий запит, але без водоспаду.

    Країновий підсумок = зона + GEO + мова. Тут лишається сама зона (усі ccTLD
    країни), решта фільтрів запиту зберігається."""
    query = await _current_query(state)
    if query is None or query.country is None:
        await callback.answer("Спочатку зробіть запит по країні", show_alert=True)
        return

    zone_query = query.replace(country=None, zones=tuple(query.country.zones))
    await state.update_data(**query_to_state(zone_query))
    await callback.answer()
    await show_result(callback, services, zone_query, callback.from_user.id)


@router.callback_query(F.data == "res:lang")
async def show_language_breakdown(
    callback: CallbackQuery, services: BotServices, state: FSMContext
) -> None:
    """Розподіл групи по мовах."""
    query = await _current_query(state)
    if query is None:
        await callback.answer("Спочатку зробіть запит", show_alert=True)
        return

    executed = await execute(services, query)
    text = render_breakdown("🗣 Мови всередині цієї групи", executed.result.language_breakdown)

    if callback.message:
        await callback.message.answer(text, reply_markup=result_menu(query.section_key))
    await callback.answer()


@router.callback_query(F.data == "res:nearby")
async def show_nearby(callback: CallbackQuery, services: BotServices, state: FSMContext) -> None:
    """«Підібрати близькі донори» — суміжні гео й м'якші вимоги."""
    query = await _current_query(state)
    if query is None:
        await callback.answer("Спочатку зробіть запит", show_alert=True)
        return

    executed = await execute(services, query)
    text = render_recommendations(executed.recommendations)

    if not text:
        text = (
            "Близьких варіантів не знайшлося.\n\n"
            "Спробуйте пом'якшити фільтри або обрати іншу країну."
        )

    if callback.message:
        await callback.message.answer(text, reply_markup=result_menu(query.section_key))
    await callback.answer()


@router.callback_query(F.data == "res:filter")
async def add_filter(callback: CallbackQuery, state: FSMContext) -> None:
    """«Додати фільтр» — веде в майстер, зберігаючи вже обране.

    Усе, що вже було в запиті, одразу позначається як успадковане: далі
    в резюме буде видно, що прийшло з попереднього запиту, а що додали
    щойно.
    """
    from app.bot.keyboards import wizard_traffic
    from app.bot.states import FRESH_KEY, Wizard

    query = await _current_query(state)
    if query is None:
        await callback.answer("Спочатку зробіть запит", show_alert=True)
        return

    await state.update_data(**{FRESH_KEY: []})
    await state.set_state(Wizard.traffic)
    await safe_edit(
        callback,
        f"📊 <b>Оберіть фільтр по трафіку</b>\n\n<i>Поточний запит: {query.describe()}</i>",
        wizard_traffic(),
    )
    await callback.answer()
