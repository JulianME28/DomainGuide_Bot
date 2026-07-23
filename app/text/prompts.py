"""Тексти інтерактивних підказок (не карток результату).

Поки що тут одне: підказка «ви переплутали режим». Коли в мовному запиті
вводять доменну зону чи країну (або навпаки — назву мови в країновому),
бот не мовчить нулем, а пояснює і пропонує два варіанти. Самі кнопки —
у `app.bot.keyboards`; звідти й звідси підписи однакові, бо беруться з
тих самих хелперів нижче.
"""

from __future__ import annotations

from app.dictionary.resolver import CrossModeHint
from app.text.cards import escape

COUNTRY_TAG = "(країна)"
LANGUAGE_TAG = "(мова)"


def country_label(hint: CrossModeHint) -> str:
    """Підпис країнового варіанта: «🇺🇦 Україна (країна)»."""
    country = hint.country
    if country is None:
        return ""
    return f"{country.flag} {country.name_uk} {COUNTRY_TAG}"


def language_label(hint: CrossModeHint) -> str:
    """Підпис мовного варіанта: «українська (мова)»."""
    language = hint.language
    if language is None:
        return ""
    return f"{language.name_uk} {LANGUAGE_TAG}"


def cross_mode_prompt(hint: CrossModeHint, *, mode: str) -> str:
    """Текст підказки. mode — режим, у якому користувач зараз вводить.

    mode="language" — вводили в мовному режимі, а це виявилась країна/зона.
    mode="country"  — вводили в країновому режимі, а це виявилась мова.
    """
    quote = escape(hint.query_text)

    if mode == "language":
        kind = "доменна зона" if hint.via_zone else "країна"
        return (
            f"«{quote}» — це {kind}, а не мова. Можливо, ви мали на увазі: "
            f"{country_label(hint)} або {language_label(hint)}?"
        )

    # mode == "country": ввели мову там, де бот чекав країну.
    country_part = country_label(hint) if hint.country else "конкретну країну"
    return (
        f"«{quote}» — це мова, а не країна. Можливо, ви мали на увазі: "
        f"{language_label(hint)} або {country_part}?"
    )
