"""Запуск бота.

    .venv\\Scripts\\python.exe -m app.main

Що відбувається при старті:

  1. читаються налаштування з .env (без них бот не стартує);
  2. читається карта колонок і перевіряється whitelist;
  3. дані з Google Sheets прогріваються в пам'ять;
  4. вмикаються захисні шари (доступ, ліміт, обробка помилок);
  5. бот починає слухати повідомлення.

Помилка в налаштуваннях зупиняє запуск і пояснює, що виправити.
А от недоступна база бот НЕ зупиняє: він підніметься й чесно повідомить,
що з базою негаразд. Працюючий бот кращий за бот, який не запустився.
"""

from __future__ import annotations

import asyncio
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.context import BotServices
from app.bot.handlers import build_router
from app.bot.middlewares import setup_middlewares
from app.data.columns import ColumnsConfigError, load_columns_config
from app.data.repository import DonorRepository
from app.data.sheets import SheetsReader
from app.llm.service import build_ai_service
from app.logging_setup import get_logger, setup_logging
from app.settings import Settings, SettingsError, load_settings

logger = get_logger(__name__)


def build_services(settings: Settings) -> BotServices:
    """Збирає все, що потрібно боту для роботи."""
    columns = load_columns_config()

    reader = SheetsReader(
        spreadsheet_id=settings.spreadsheet_id,
        credentials_file=settings.credentials_file,
    )
    repository = DonorRepository(reader, columns, ttl_seconds=settings.cache_ttl_seconds)

    # ШІ — лише якщо у .env вписано ключ. Інакше None, і бот працює словником.
    ai = build_ai_service(settings)
    if ai is not None:
        logger.info(
            "ШІ увімкнено: провайдер %s, модель %s", settings.llm_provider, settings.llm_model
        )

    return BotServices(settings=settings, columns=columns, repository=repository, ai=ai)


async def run() -> None:
    """Основний цикл роботи бота."""
    settings = load_settings()
    setup_logging(settings.log_level)

    logger.info("Запуск DomainGuide Bot")
    services = build_services(settings)

    logger.info(
        "Доступ дозволено для %d користувачів, з них адмінів: %d",
        len(settings.allowed_user_ids),
        len(settings.admin_user_ids),
    )

    # Прогрів кешу. Якщо база недоступна — бот усе одно стартує.
    await services.repository.warmup()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())

    # Сервіси потрапляють в обробники як звичайний аргумент services.
    dispatcher["services"] = services

    setup_middlewares(dispatcher, services)
    dispatcher.include_router(build_router())

    me = await bot.get_me()
    logger.info("Бот @%s готовий до роботи", me.username)

    try:
        # drop_pending_updates=True — не відповідаємо на повідомлення, які
        # накопичилися, поки бот був вимкнений.
        await dispatcher.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()
        logger.info("Бот зупинено")


def main() -> int:
    """Точка входу з обробкою зрозумілих помилок."""
    try:
        asyncio.run(run())
    except (SettingsError, ColumnsConfigError) as exc:
        # Помилки налаштувань показуємо людині просто й без стектрейсу:
        # тут важливо не «де впало», а «що виправити».
        print("\n" + "=" * 60)
        print("НЕ ВДАЛОСЯ ЗАПУСТИТИ БОТА")
        print("=" * 60)
        print(f"\n{exc}\n")
        return 1
    except KeyboardInterrupt:
        print("\nЗупинено користувачем.")
        return 0
    except Exception as exc:  # непередбачене — показуємо тип і суть
        logger.exception("Несподівана помилка під час запуску")
        print(f"\nНесподівана помилка: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
