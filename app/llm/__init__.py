"""Підключення ШІ для розбору РОЗМИТИХ вільних запитів.

Це РЕЗЕРВ, а не основний шлях. Усе, що розбирає словник (dictionary/) —
працює миттєво, безкоштовно й без інтернету. ШІ вмикається лише коли
детермінований розбір не зрозумів запит, і лише якщо у .env вписано ключ.

Непорушне правило (ТЗ §5): ШІ бачить ЛИШЕ текст запиту користувача й перелік
доступних фільтрів, країн і мов. Донорів, доменів і будь-якого вмісту бази він
не бачить НІКОЛИ. ШІ повертає структурований фільтр, який backend перевіряє по
whitelist перед виконанням — жодних довільних запитів від ШІ до даних.
"""

from __future__ import annotations

from app.llm.interpreter import LLMInterpreter, build_catalog
from app.llm.provider import AnthropicProvider, LLMError
from app.llm.service import AIService, build_ai_service

__all__ = [
    "AIService",
    "AnthropicProvider",
    "LLMError",
    "LLMInterpreter",
    "build_ai_service",
    "build_catalog",
]
