"""Кнопки бота.

Основний спосіб роботи — кнопки, а не текст (вимога ТЗ, розділи 21 і 29).
Типовий запит має робитися взагалі без клавіатури.

Формат даних кнопки — рядок із двокрапками:

    menu:main               головне меню
    menu:section:magic      меню розділу «Меджик»
    q:country:magic:de      швидкий запит: Меджик, Німеччина
    wizard:traffic:10       крок майстра: трафік від 10
    res:geo                 кнопка під результатом: уточнити гео

Telegram обмежує ці дані 64 байтами, тому вони короткі.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.analytics.query import DIMENSION_ACCUSATIVE, Dimension
from app.dictionary.countries import country_by_code

# Країни, які виносимо на кнопки. Решта — через «Ввести вручну».
POPULAR_COUNTRIES = ("gb", "us", "de", "fr", "es", "it", "ca", "au", "pl", "nl", "br", "tr")

# Порядок кнопок «❌ Прибрати …» — сталий, щоб вони не стрибали між показами.
DROP_ORDER = (
    Dimension.COUNTRY,
    Dimension.GEO,
    Dimension.LANGUAGE,
    Dimension.TRAFFIC,
    Dimension.DR,
    Dimension.SPAM,
)

# Швидкі варіанти для метрик у майстрі. DR і трафік — пороги «від N».
TRAFFIC_OPTIONS = (1, 10, 50, 100, 500)
DR_OPTIONS = (10, 20, 30, 40, 50)
# Заспамленість — пороги «до N» (менше = краще), під реальний розподіл «Морд»
# (медіана заспамлених 2; база скошена вліво). Стовпець «вихідні» не фільтрується.
SPAM_OPTIONS = (5, 20, 50, 100)


def main_menu(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    """Головне меню (ТЗ, розділ 22)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🪄 Меджик", callback_data="menu:section:magic")
    builder.button(text="🧱 Морди", callback_data="menu:section:mordy")
    builder.button(text="📩 Сабміти", callback_data="menu:section:submits")
    builder.button(text="🧠 Індивідуальний запит", callback_data="ai:start")
    builder.button(text="💬 ШІ-консультант", callback_data="ai:chat")
    builder.button(text="📊 Статус", callback_data="menu:status")
    builder.button(text="❓ Допомога", callback_data="menu:help")
    if is_admin:
        builder.button(text="⚙️ Адмінка", callback_data="admin:menu")

    builder.adjust(2, 1, 2, 2, 1)
    return builder.as_markup()


def ai_chat_menu() -> InlineKeyboardMarkup:
    """Керування живою розмовою: скинути лише контекст або вийти до меню."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Новий діалог", callback_data="ai:chat:new")
    builder.button(text="⬅️ До меню", callback_data="menu:main")
    builder.adjust(1, 1)
    return builder.as_markup()


def section_menu(section_key: str) -> InlineKeyboardMarkup:
    """Меню всередині бази (ТЗ, розділ 23)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🌍 Донори по країні", callback_data=f"pick:country:{section_key}")
    builder.button(text="🗣 Донори по мові", callback_data=f"ask:language:{section_key}")
    builder.button(text="🔗 Донори по доменній зоні", callback_data=f"ask:zone:{section_key}")
    builder.button(text="📈 DR / трафік", callback_data=f"wizard:start:{section_key}")
    builder.button(text="🧩 Комбінація метрик", callback_data=f"wizard:start:{section_key}")
    builder.button(text="🤖 Вільний запит", callback_data=f"ask:free:{section_key}")
    builder.button(text="⬅️ Назад", callback_data="menu:main")

    builder.adjust(1)
    return builder.as_markup()


def country_picker(section_key: str) -> InlineKeyboardMarkup:
    """Кнопки популярних країн + ручне введення."""
    builder = InlineKeyboardBuilder()
    for code in POPULAR_COUNTRIES:
        country = country_by_code(code)
        if country:
            builder.button(
                text=f"{country.flag} {country.name_uk}",
                callback_data=f"q:country:{section_key}:{code}",
            )
    builder.button(text="✍️ Інша країна", callback_data=f"ask:country:{section_key}")
    builder.button(text="⬅️ Назад", callback_data=f"menu:section:{section_key}")

    builder.adjust(3, 3, 3, 3, 1, 1)
    return builder.as_markup()


def ai_retry_menu() -> InlineKeyboardMarkup:
    """Кнопки під карткою «не зрозумів»: спробувати той самий запит через ШІ."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🧠 Уточнити через ШІ", callback_data="ai:retry")
    builder.button(text="⬅️ До меню", callback_data="menu:main")
    builder.adjust(1, 1)
    return builder.as_markup()


def result_menu(
    section_key: str,
    *,
    has_recommendations: bool = True,
    has_country: bool = False,
    run_in: tuple[str, str] | None = None,
    ai_retry: bool = False,
    all_countries: bool = False,
) -> InlineKeyboardMarkup:
    """Кнопки під карткою результату (ТЗ, розділ 25).

    has_country — чи це запит про КРАЇНУ. Тоді додається кнопка «Тільки доменна
    зона»: вона перезапускає той самий запит без водоспаду (без GEO і мови),
    щоб звузити до самої зони одним дотиком.

    run_in — (ключ, назва) бази, де є відкинуті цим запитом виміри. Якщо задано,
    додається кнопка «виконати цей запит там»."""
    builder = InlineKeyboardBuilder()
    # Якщо частину запиту не зрозуміли — першою даємо кнопку «уточнити через ШІ».
    if ai_retry:
        builder.button(text="🧠 Уточнити через ШІ", callback_data="ai:retry")
    if section_key == "mordy":
        builder.button(text="🚫 Перевірка на стоп", callback_data="res:stop")
    if has_recommendations:
        builder.button(text="🔎 Підібрати близькі донори", callback_data="res:nearby")
    if run_in is not None:
        builder.button(
            text=f"🔄 Виконати цей запит у базі {run_in[1]}",
            callback_data=f"res:runin:{run_in[0]}",
        )
    if has_country:
        builder.button(text="🔗 Тільки доменна зона", callback_data="res:zoneonly")
    # Розбивка по країнах: кнопка «показати всі» (коли картка — це розбивка).
    if all_countries:
        builder.button(text="🌍 Показати всі країни", callback_data="res:allcountries")
    builder.button(text="🌍 Уточнити гео в цій групі", callback_data="res:geo")
    builder.button(text="🗣 Розподіл по мовах", callback_data="res:lang")
    builder.button(text="➕ Додати фільтр", callback_data="res:filter")
    builder.button(text="🔄 Новий запит", callback_data=f"menu:section:{section_key}")
    builder.button(text="⬅️ До меню", callback_data="menu:main")

    # Усі кнопки по одній у рядок, крім останньої пари («Новий запит» + «До меню»).
    singles = (
        3
        + int(section_key == "mordy")
        + int(has_recommendations)
        + int(has_country)
        + int(run_in is not None)
        + int(ai_retry)
        + int(all_countries)
    )
    builder.adjust(*([1] * singles), 2)
    return builder.as_markup()


def both_bases_menu(
    bases: list[tuple[str, str]], *, ai_retry: bool = False, country_breakdown: bool = False
) -> InlineKeyboardMarkup:
    """Кнопки під зведеним повідомленням по обох базах.

    bases — список (ключ, назва). За замовчуванням — «Детально по …» (повна картка
    бази); для розбивки по країнах (country_breakdown=True) — «Всі країни: …».
    ai_retry — чи додати «🧠 Уточнити через ШІ» (коли частину запиту не зрозуміли)."""
    builder = InlineKeyboardBuilder()
    if ai_retry:
        builder.button(text="🧠 Уточнити через ШІ", callback_data="ai:retry")
    for key, title in bases:
        if country_breakdown:
            builder.button(text=f"🌍 Всі країни: {title}", callback_data=f"res:allcountries:{key}")
        else:
            builder.button(text=f"📊 Детально по {title}", callback_data=f"res:detail:{key}")
    if any(key == "mordy" for key, _title in bases):
        builder.button(text="🚫 Перевірка Мордів на стоп", callback_data="res:stop")
    builder.button(text="⬅️ До меню", callback_data="menu:main")
    builder.adjust(
        *([1] * (len(bases) + int(ai_retry) + int(any(k == "mordy" for k, _ in bases)))),
        1,
    )
    return builder.as_markup()


def stop_check_menu(*, ai_retry: bool = False) -> InlineKeyboardMarkup:
    """Мінімальне меню під мультикраїнним результатом Мордів.

    ai_retry — додати «🧠 Уточнити через ШІ» (результат зі словника/вільного тексту
    й ШІ ввімкнено): дозволяє переграти той самий запит через ШІ. Раніше цього
    параметра тут не було — тому на мультикраїнній картці з «не впізнав» кнопки
    бракувало (це й був баг)."""
    builder = InlineKeyboardBuilder()
    if ai_retry:
        builder.button(text="🧠 Уточнити через ШІ", callback_data="ai:retry")
    builder.button(text="🚫 Перевірка на стоп", callback_data="res:stop")
    builder.button(text="⬅️ До меню", callback_data="menu:main")
    builder.adjust(*([1] * (2 + int(ai_retry))))
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Майстер-запит
# ---------------------------------------------------------------------------


def wizard_sections() -> InlineKeyboardMarkup:
    """Крок 1: яка база."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🪄 Меджик", callback_data="wizard:section:magic")
    builder.button(text="🧱 Морди", callback_data="wizard:section:mordy")
    builder.button(text="⬅️ Назад", callback_data="menu:main")
    builder.adjust(2, 1)
    return builder.as_markup()


def wizard_countries() -> InlineKeyboardMarkup:
    """Крок 2: КРАЇНА (саме країна, не «гео»)."""
    builder = InlineKeyboardBuilder()
    for code in POPULAR_COUNTRIES:
        country = country_by_code(code)
        if country:
            builder.button(
                text=f"{country.flag} {country.name_uk}",
                callback_data=f"wizard:country:{code}",
            )
    builder.button(text="✍️ Ввести вручну", callback_data="wizard:country:manual")
    builder.button(text="➡️ Пропустити", callback_data="wizard:country:skip")
    builder.row()
    _add_navigation(builder, back="wizard:start")
    builder.adjust(3, 3, 3, 3, 2, 3)
    return builder.as_markup()


def wizard_geo() -> InlineKeyboardMarkup:
    """Крок «ГЕО (країна трафіку)» — фільтр по колонці GEO.

    Країну вводять текстом (як на кроці країни), а тут — лише «не важливо»
    й навігація. GEO є в обох базах, тож крок показуємо для обох."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Не важливо", callback_data="wizard:geo:any")
    _add_navigation(builder, back="wizard:back:country")
    builder.adjust(1, 3)
    return builder.as_markup()


def wizard_traffic(*, back: str = "country") -> InlineKeyboardMarkup:
    """Крок 3: трафік. back — попередній крок (для баз із GEO це «geo»)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Не важливо", callback_data="wizard:traffic:any")
    for value in TRAFFIC_OPTIONS:
        builder.button(text=f"Від {value}", callback_data=f"wizard:traffic:{value}")
    builder.button(text="✍️ Ввести вручну", callback_data="wizard:traffic:manual")
    _add_navigation(builder, back=f"wizard:back:{back}")
    builder.adjust(3, 3, 1, 3)
    return builder.as_markup()


def wizard_dr() -> InlineKeyboardMarkup:
    """Крок 4: DR."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Не важливо", callback_data="wizard:dr:any")
    for value in DR_OPTIONS:
        builder.button(text=f"Від {value}", callback_data=f"wizard:dr:{value}")
    builder.button(text="✍️ Ввести вручну", callback_data="wizard:dr:manual")
    _add_navigation(builder, back="wizard:back:traffic")
    builder.adjust(3, 3, 1, 3)
    return builder.as_markup()


def wizard_spam() -> InlineKeyboardMarkup:
    """Крок «Заспамленість» (стовпець G) — у КІЛЬКОСТІ заспамлених лінків, «Морди».

    Менше = краще, тож пороги — «до N» (максимум) плюс «не важливо». Окремого
    кроку «вихідні» немає: стовпець F числом не фільтрується (лише відсів мертвих).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="Не важливо", callback_data="wizard:spam:any")
    for value in SPAM_OPTIONS:
        builder.button(text=f"До {value}", callback_data=f"wizard:spam:{value}")
    builder.button(text="✍️ Ввести вручну", callback_data="wizard:spam:manual")
    _add_navigation(builder, back="wizard:back:dr")
    builder.adjust(3, 2, 1, 3)
    return builder.as_markup()


def wizard_confirm(droppable: Iterable[str] = (), *, back: str = "dr") -> InlineKeyboardMarkup:
    """Крок 5: резюме перед запуском (ТЗ, розділ 30).

    droppable — виміри, які лишилися з попереднього запиту. Для кожного
    з'являється своя кнопка «❌ Прибрати …», щоб зайвий фільтр знімався
    одним дотиком, а не через повне скидання всього запиту.

    back — крок, на який веде «Назад». Для «Морд» це «spam» (останній перед
    резюме), для «Меджика» — «dr» (кроків спаму/вихідних там немає).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Запустити перевірку", callback_data="wizard:run")

    drop_buttons = 0
    for dimension in DROP_ORDER:
        if dimension in droppable:
            title = DIMENSION_ACCUSATIVE[dimension]
            builder.button(text=f"❌ Прибрати {title}", callback_data=f"wizard:drop:{dimension}")
            drop_buttons += 1

    builder.button(text="🗣 Додати мову", callback_data="wizard:addlang")
    builder.button(text="⬅️ Назад", callback_data=f"wizard:back:{back}")
    builder.button(text="🔄 Скинути все", callback_data="wizard:reset")

    # Кнопки скидання йдуть по одній у рядок — так їх важче натиснути випадково.
    builder.adjust(1, *([1] * drop_buttons), 1, 2)
    return builder.as_markup()


def _add_navigation(builder: InlineKeyboardBuilder, *, back: str) -> None:
    """Кнопки «Назад / Скинути / До меню» — вони мають бути на кожному кроці."""
    builder.button(text="⬅️ Назад", callback_data=back)
    builder.button(text="🔄 Скинути", callback_data="wizard:reset")
    builder.button(text="🏠 До меню", callback_data="menu:main")


def cross_mode_keyboard(section_key: str, hint, *, mode: str) -> InlineKeyboardMarkup:
    """Кнопки підказки «ви переплутали режим».

    Порядок кнопок збігається з текстом підказки: першим іде «природний»
    варіант (те, чим введене є насправді), другим — той самий запит у
    поточному режимі. Для мов кількох країн конкретної країни немає — тоді
    друга кнопка веде у звичайний вибір країни.
    """
    from app.text.prompts import country_label, language_label

    builder = InlineKeyboardBuilder()

    if mode == "language":
        # Ввели країну/зону в мовному режимі: спершу країна, потім її мова.
        builder.button(
            text=country_label(hint),
            callback_data=f"q:country:{section_key}:{hint.country.code}",
        )
        builder.button(
            text=language_label(hint),
            callback_data=f"q:lang:{section_key}:{hint.language.code}",
        )
    else:
        # Ввели мову в країновому режимі: спершу мова, потім країна.
        builder.button(
            text=language_label(hint),
            callback_data=f"q:lang:{section_key}:{hint.language.code}",
        )
        if hint.country is not None:
            builder.button(
                text=country_label(hint),
                callback_data=f"q:country:{section_key}:{hint.country.code}",
            )
        else:
            builder.button(text="🌍 Обрати країну", callback_data=f"pick:country:{section_key}")

    builder.button(text="🔄 Скинути", callback_data="wizard:reset")
    builder.button(text="🏠 До меню", callback_data="menu:main")
    builder.adjust(1, 1, 2)
    return builder.as_markup()


def cancel_only() -> InlineKeyboardMarkup:
    """Клавіатура для кроку з ручним введенням."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Скинути", callback_data="wizard:reset")
    builder.button(text="🏠 До меню", callback_data="menu:main")
    builder.adjust(2)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ До меню", callback_data="menu:main")
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Адмінка
# ---------------------------------------------------------------------------


def admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📡 Статус баз", callback_data="admin:status")
    builder.button(text="🗺 Карта колонок", callback_data="admin:columns")
    builder.button(text="🧪 Тестовий запит", callback_data="admin:test")
    builder.button(text="👥 Дозволені користувачі", callback_data="admin:users")
    builder.button(text="📜 Лог останніх запитів", callback_data="admin:log")
    builder.button(text="🔄 Оновити дані", callback_data="admin:refresh")
    builder.button(text="⬅️ До меню", callback_data="menu:main")
    builder.adjust(2, 1, 1, 1, 1, 1)
    return builder.as_markup()


def access_users_menu(user_ids: Iterable[int]) -> InlineKeyboardMarkup:
    """Список тих, хто зайшов за КОДОМ, із кнопкою «❌ Прибрати» на кожного.

    Прибирає лише динамічний доступ (за кодом); статичний список .env через бота
    не чіпається. Порожній список → лише кнопка повернення в адмін-меню."""
    builder = InlineKeyboardBuilder()
    for user_id in user_ids:
        builder.button(text=f"❌ Прибрати {user_id}", callback_data=f"admin:revoke:{user_id}")
    builder.button(text="⬅️ Адмін-меню", callback_data="admin:menu")
    builder.adjust(1)
    return builder.as_markup()
