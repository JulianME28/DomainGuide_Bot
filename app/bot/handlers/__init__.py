"""Обробники подій бота.

Розділені за призначенням, щоб кожен файл лишався оглядним:

    common.py    /start, /help, головне меню, статус
    sections.py  меню баз і швидкі запити по країні, мові, зоні
    wizard.py    покроковий майстер-запит
    ai.py        індивідуальний запит через ШІ (завжди ШІ) + «уточнити через ШІ»
    freeform.py  запити вільним текстом
    admin.py     адмін-меню
"""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import admin, ai, common, freeform, sections, wizard


def build_router() -> Router:
    """Збирає всі обробники в один маршрутизатор.

    Порядок має значення: вільний текст іде ОСТАННІМ, бо він приймає
    будь-яке повідомлення. Якби він стояв вище, то перехоплював би відповіді
    на кроках майстра — зокрема й стан «Індивідуальний запит» (ai).
    """
    router = Router(name="root")
    router.include_router(common.router)
    router.include_router(admin.router)
    router.include_router(sections.router)
    router.include_router(wizard.router)
    router.include_router(ai.router)
    router.include_router(freeform.router)
    return router
