"""Налаштування бота — читаються з файлу .env.

Головне правило: у коді немає жодного секрету. Токен, ID таблиці, список
дозволених користувачів — усе живе в .env, який не потрапляє в git.

Якщо в .env щось не так, бот не стартує і пише зрозумілою мовою, що саме
виправити. Це краще, ніж падати посеред роботи з незрозумілою помилкою.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Корінь проєкту — тека, де лежать .env, config/ і credentials.json.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SettingsError(RuntimeError):
    """Помилка в налаштуваннях. Текст цієї помилки бачить людина, тому він
    має бути зрозумілий і підказувати, що робити."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Усі налаштування бота в одному місці."""

    # repr=False — щоб токен випадково не потрапив у лог або в текст помилки.
    bot_token: str = field(repr=False)
    data_backend: str
    spreadsheet_id: str
    credentials_file: Path
    allowed_user_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    llm_provider: str
    cache_ttl_seconds: int
    rate_limit_requests: int
    rate_limit_window_seconds: int
    log_level: str

    # --- ШІ для вільних запитів (за замовчуванням вимкнено) ------------------
    # repr=False — ключ ШІ, як і токен бота, не має потрапити в лог чи в текст
    # помилки. Порожній ключ = ШІ вимкнено, навіть якщо LLM_PROVIDER=anthropic.
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_timeout_seconds: int = 20
    llm_calls_limit: int = 10
    llm_window_seconds: int = 3600

    @property
    def llm_enabled(self) -> bool:
        """Чи ШІ реально ввімкнено: обрано провайдера anthropic І є ключ.

        Немає ключа — ШІ вимкнено (тихий фолбек на словниковий розбір), навіть
        якщо провайдера вказали. Це навмисно: бот має працювати ще до того, як
        власниця впише ключ."""
        return self.llm_provider == "anthropic" and bool(self.llm_api_key)

    def is_allowed(self, user_id: int) -> bool:
        """Чи можна цьому Telegram ID користуватися ботом.

        Адмін проходить завжди, навіть якщо його забули дописати в
        ALLOWED_USER_IDS. Перевірка стоїть саме тут, а не лише при читанні
        .env: так правило «адмін завжди має доступ» діє за будь-яких умов і
        не може загубитися.
        """
        return user_id in self.allowed_user_ids or user_id in self.admin_user_ids

    def is_admin(self, user_id: int) -> bool:
        """Чи має цей Telegram ID доступ до адмін-меню.

        Адмін завжди має і звичайний доступ теж — інакше вийшла б дивна
        ситуація «адмін є, а користуватися ботом не може».
        """
        return user_id in self.admin_user_ids


def _parse_ids(raw: str) -> frozenset[int]:
    """Перетворює рядок "111,222, 333" на набір чисел {111, 222, 333}.

    Зайві пробіли й порожні шматки просто ігноруються — щоб випадкова кома
    в кінці рядка не ламала запуск.
    """
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if not chunk.lstrip("-").isdigit():
            raise SettingsError(
                f"У списку Telegram ID знайдено значення «{chunk}», яке не є числом. "
                "Треба вказувати лише числові ID через кому, наприклад: 850410806,123456789"
            )
        ids.add(int(chunk))
    return frozenset(ids)


def _parse_int(raw: str, name: str, default: int, minimum: int = 1) -> int:
    """Читає число з .env. Якщо порожньо — бере значення за замовчуванням."""
    raw = raw.strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(
            f"Налаштування {name} має бути числом, а зараз там «{raw}». "
            f"Або впишіть число, або залиште порожнім (тоді буде {default})."
        ) from None
    if value < minimum:
        raise SettingsError(f"Налаштування {name} має бути не менше {minimum}, а зараз {value}.")
    return value


def load_settings(env_file: str | Path | None = None, *, require_token: bool = True) -> Settings:
    """Читає .env і збирає з нього об'єкт Settings.

    require_token=False потрібен тестам і адмін-перевіркам: там токен не
    потрібен, а решту налаштувань перевірити хочеться.
    """
    env_path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    elif require_token:
        raise SettingsError(
            f"Не знайдено файл налаштувань {env_path}.\n"
            "Створіть його: скопіюйте .env.example у .env і заповніть значення."
        )

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and not bot_token:
        raise SettingsError(
            "У файлі .env не заповнено BOT_TOKEN.\n"
            "Напишіть у Telegram боту @BotFather, надішліть /newbot, "
            "скопіюйте виданий токен і вставте його в рядок BOT_TOKEN=."
        )

    data_backend = os.getenv("DATA_BACKEND", "sheets").strip().lower() or "sheets"
    if data_backend != "sheets":
        raise SettingsError(
            f"DATA_BACKEND=«{data_backend}» поки не підтримується. Допустиме значення: sheets."
        )

    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()
    if require_token and not spreadsheet_id:
        raise SettingsError(
            "У файлі .env не заповнено GOOGLE_SPREADSHEET_ID.\n"
            "Це довгий шматок із посилання на таблицю: "
            "docs.google.com/spreadsheets/d/ЦЕЙ_ШМАТОК/edit"
        )

    credentials_raw = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json").strip()
    credentials_file = Path(credentials_raw)
    if not credentials_file.is_absolute():
        credentials_file = PROJECT_ROOT / credentials_file

    allowed = _parse_ids(os.getenv("ALLOWED_USER_IDS", ""))
    admins = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))
    # Адмін автоматично отримує і звичайний доступ.
    allowed = allowed | admins

    if require_token and not allowed:
        raise SettingsError(
            "У файлі .env порожній ALLOWED_USER_IDS — тоді бот не відповідатиме нікому.\n"
            "Дізнайтеся свій Telegram ID у бота @userinfobot і впишіть його."
        )

    ttl_minutes = _parse_int(os.getenv("CACHE_TTL_MINUTES", ""), "CACHE_TTL_MINUTES", 30)

    return Settings(
        bot_token=bot_token,
        data_backend=data_backend,
        spreadsheet_id=spreadsheet_id,
        credentials_file=credentials_file,
        allowed_user_ids=allowed,
        admin_user_ids=admins,
        llm_provider=os.getenv("LLM_PROVIDER", "none").strip().lower() or "none",
        cache_ttl_seconds=ttl_minutes * 60,
        rate_limit_requests=_parse_int(
            os.getenv("RATE_LIMIT_REQUESTS", ""), "RATE_LIMIT_REQUESTS", 20
        ),
        rate_limit_window_seconds=_parse_int(
            os.getenv("RATE_LIMIT_WINDOW_SECONDS", ""), "RATE_LIMIT_WINDOW_SECONDS", 60
        ),
        log_level=(os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=(os.getenv("LLM_MODEL", "").strip() or "claude-haiku-4-5-20251001"),
        llm_timeout_seconds=_parse_int(
            os.getenv("LLM_TIMEOUT_SECONDS", ""), "LLM_TIMEOUT_SECONDS", 20
        ),
        llm_calls_limit=_parse_int(os.getenv("LLM_CALLS_LIMIT", ""), "LLM_CALLS_LIMIT", 10),
        llm_window_seconds=_parse_int(
            os.getenv("LLM_WINDOW_SECONDS", ""), "LLM_WINDOW_SECONDS", 3600
        ),
    )
