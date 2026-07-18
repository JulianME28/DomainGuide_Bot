"""Читання Google Sheets — тільки читання, тільки дозволені колонки.

Два рівні захисту:

1. Scope доступу — `spreadsheets.readonly`. Навіть якщо в коді була б помилка,
   Google фізично не дозволить боту нічого змінити в таблиці.

2. Whitelist колонок. Бот не завантажує аркуш цілком. Він спершу читає рядок
   заголовків, знаходить потрібні стовпчики й запитує В ГУГЛА ЛИШЕ ЇХ.
   Дані з інших колонок не те що не зберігаються — вони навіть не залишають
   сервер Google.

Функції тут звичайні (не async): мережеві виклики блокуючі. Асинхронність
додає шар вище — repository.py, через asyncio.to_thread.
"""

from __future__ import annotations

from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from app.data.columns import SectionConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)

# ТІЛЬКИ читання. Змінити щось у таблиці бот не може за визначенням.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetsError(RuntimeError):
    """Проблема з доступом до таблиці. Текст пишеться зрозумілою мовою,
    бо його показують адміну в боті."""


def _column_letter(index: int) -> str:
    """Номер колонки → літера в таблиці. 1 → A, 27 → AA."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _match_header(headers: list[str], wanted: str) -> int | None:
    """Шукає колонку за назвою. Повертає номер (з 1) або None.

    Спершу шукає точний збіг, потім — без урахування регістру й пробілів,
    бо в заголовках таблиці теж бувають хвостові пробіли.
    """
    for position, header in enumerate(headers, start=1):
        if header == wanted:
            return position
    target = wanted.strip().casefold()
    for position, header in enumerate(headers, start=1):
        if header.strip().casefold() == target:
            return position
    return None


class SheetsReader:
    """Обгортка над gspread: відкриває таблицю й читає дозволені колонки."""

    def __init__(self, spreadsheet_id: str, credentials_file: Path) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._credentials_file = Path(credentials_file)
        self._client: gspread.Client | None = None

    # -- підключення ---------------------------------------------------------

    def _connect(self) -> gspread.Client:
        """Підключається до Google один раз і далі перевикористовує з'єднання."""
        if self._client is not None:
            return self._client

        if not self._credentials_file.exists():
            raise SheetsError(
                f"Не знайдено файл-ключ Google: {self._credentials_file}\n"
                "Перевірте значення GOOGLE_CREDENTIALS_FILE у файлі .env."
            )

        try:
            credentials = Credentials.from_service_account_file(
                str(self._credentials_file), scopes=SCOPES
            )
            self._client = gspread.authorize(credentials)
        except Exception as exc:
            raise SheetsError(
                "Не вдалося авторизуватися в Google за ключем сервіс-акаунта.\n"
                f"Деталі: {type(exc).__name__}: {exc}"
            ) from exc

        return self._client

    def _open_worksheet(self, sheet_name: str) -> gspread.Worksheet:
        """Відкриває конкретний аркуш таблиці."""
        client = self._connect()
        try:
            spreadsheet = client.open_by_key(self._spreadsheet_id)
        except gspread.exceptions.APIError as exc:
            raise SheetsError(
                "Google не дав доступ до таблиці.\n"
                "Найчастіша причина: таблицею не поділилися з сервіс-акаунтом. "
                "Відкрийте таблицю → «Поділитися» → додайте пошту з credentials.json "
                "(поле client_email) з правами «Переглядач».\n"
                f"Деталі: {exc}"
            ) from exc
        except Exception as exc:
            raise SheetsError(f"Не вдалося відкрити таблицю: {type(exc).__name__}: {exc}") from exc

        try:
            return spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound as exc:
            available = ", ".join(ws.title for ws in spreadsheet.worksheets())
            raise SheetsError(
                f"У таблиці немає аркуша «{sheet_name}».\n"
                f"Наявні аркуші: {available}\n"
                "Перевірте назву в config/columns.toml (важливі регістр і мова літер)."
            ) from exc

    # -- читання -------------------------------------------------------------

    def read_section(self, section: SectionConfig) -> list[dict[str, str]]:
        """Читає дозволені колонки одного розділу.

        Повертає список рядків виду {"domain": "...", "dr": "...", ...},
        де ключі — це РОЛІ, а не назви колонок. Далі коду байдуже, як
        стовпчик називається в таблиці.

        Порожній аркуш — це не помилка, а порожній список.
        """
        if not section.reads_data:
            return []

        worksheet = self._open_worksheet(section.sheet)

        # Крок 1: заголовки. Один невеликий запит.
        try:
            headers = worksheet.row_values(1)
        except Exception as exc:
            raise SheetsError(
                f"Не вдалося прочитати заголовки аркуша «{section.sheet}»: {exc}"
            ) from exc

        if not headers:
            logger.info("Аркуш «%s» порожній — 0 донорів.", section.sheet)
            return []

        # Крок 2: знаходимо номери потрібних колонок.
        positions: dict[str, int] = {}
        for role, header in section.columns.items():
            position = _match_header(headers, header)
            if position is None:
                raise SheetsError(
                    f"На аркуші «{section.sheet}» немає колонки «{header}» (роль {role}).\n"
                    f"Наявні колонки: {', '.join(h for h in headers if h.strip())}\n"
                    "Виправте назву в config/columns.toml."
                )
            positions[role] = position

        # Крок 3: запитуємо в Google ЛИШЕ ці колонки, з другого рядка (без заголовків).
        roles = list(positions)
        ranges = []
        for role in roles:
            letter = _column_letter(positions[role])
            ranges.append(f"{letter}2:{letter}")  # з другого рядка й до кінця колонки

        try:
            columns_data = worksheet.batch_get(ranges, major_dimension="COLUMNS")
        except Exception as exc:
            raise SheetsError(
                f"Не вдалося прочитати дані з аркуша «{section.sheet}»: {type(exc).__name__}: {exc}"
            ) from exc

        # Крок 4: складаємо колонки назад у рядки.
        # Google обрізає порожні хвости, тому колонки можуть бути різної довжини —
        # вирівнюємо їх порожніми значеннями.
        values_by_role: dict[str, list[str]] = {}
        for role, block in zip(roles, columns_data, strict=False):
            values_by_role[role] = list(block[0]) if block else []

        height = max((len(v) for v in values_by_role.values()), default=0)
        if height == 0:
            logger.info("Аркуш «%s» без рядків даних — 0 донорів.", section.sheet)
            return []

        rows: list[dict[str, str]] = []
        for index in range(height):
            rows.append(
                {
                    role: (values[index] if index < len(values) else "")
                    for role, values in values_by_role.items()
                }
            )

        logger.info("Аркуш «%s»: прочитано %d рядків.", section.sheet, len(rows))
        return rows

    def probe(self, section: SectionConfig) -> int:
        """Швидка перевірка для адмінки: скільки рядків на аркуші.

        Не тягне самі дані — питає лише розмір.
        """
        if not section.reads_data:
            return 0
        worksheet = self._open_worksheet(section.sheet)
        return max(worksheet.row_count - 1, 0)
