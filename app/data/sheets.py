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

import json
import socket
import time
from collections.abc import Callable
from pathlib import Path

import gspread
import requests
from google.oauth2.service_account import Credentials

from app.data.columns import SectionConfig
from app.logging_setup import get_logger

logger = get_logger(__name__)

# ТІЛЬКИ читання. Змінити щось у таблиці бот не може за визначенням.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# ---------------------------------------------------------------------------
# Повтор спроб при мережевих збоях.
#
# Google інколи рве з'єднання (ConnectionResetError, WinError 10054) або
# ненадовго відповідає 5xx. Це минущі помилки: варто просто спробувати ще
# раз. А от 403, відсутній аркуш чи колонка від повтору не зникнуть — їх
# показуємо одразу.
#
# 3 спроби. Паузи наростають: 1 с перед 2-ю спробою, 3 с перед 3-ю.
# (6 с у списку — про запас, якщо колись знадобиться 4-та спроба.)
# ---------------------------------------------------------------------------
_DEFAULT_BACKOFFS: tuple[float, ...] = (1.0, 3.0, 6.0)
_DEFAULT_MAX_ATTEMPTS = 3

# Типи помилок, які вважаємо тимчасовими й повторюємо.
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,  # зокрема ConnectionResetError (WinError 10054), ConnectionAbortedError
    TimeoutError,  # мережеві таймаути (у py3.10+ це і socket.timeout)
    socket.timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

# HTTP-статуси, які означають «спробуй ще раз»: перевантаження й тимчасові збої.
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Текстові ознаки мережевого збою — запобіжник на випадок незвичних обгорток.
_TRANSIENT_HINTS = ("10054", "connection reset", "connection aborted", "timed out", "broken pipe")


class SheetsError(RuntimeError):
    """Проблема з доступом до таблиці. Текст пишеться зрозумілою мовою,
    бо його показують адміну в боті."""


def _api_status(exc: BaseException) -> int | None:
    """Дістає HTTP-статус із помилки gspread, якщо він там є."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _is_transient(exc: BaseException) -> bool:
    """Чи ця помилка тимчасова — тобто чи має сенс повторити спробу.

    Перевіряє не лише саму помилку, а й увесь ланцюжок причин
    (`__cause__`/`__context__`): requests часто загортає мережевий збій у
    кілька рівнів, і справжня причина ховається всередині.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _TRANSIENT_TYPES):
            return True
        if _api_status(current) in _TRANSIENT_STATUS:
            # _api_status повертає None для всього, що не є HTTP-помилкою gspread,
            # а None у наборі статусів немає — тож зайвого спрацювання не буде.
            return True
        current = current.__cause__ or current.__context__

    text = str(exc).lower()
    return any(hint in text for hint in _TRANSIENT_HINTS)


def _brief(exc: BaseException | None) -> str:
    """Короткий опис помилки для лога: «ConnectionResetError: ...»."""
    if exc is None:
        return "невідома помилка"
    return f"{type(exc).__name__}: {exc}"


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

    def __init__(
        self,
        spreadsheet_id: str,
        credentials_file: Path,
        *,
        sleeper: Callable[[float], None] | None = None,
        backoffs: tuple[float, ...] = _DEFAULT_BACKOFFS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._credentials_file = Path(credentials_file)
        self._client: gspread.Client | None = None
        # sleeper винесено параметром, щоб тести не чекали справжні секунди.
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._backoffs = tuple(backoffs)
        self._max_attempts = max(1, max_attempts)

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

    @property
    def service_account_email(self) -> str:
        """Пошта сервіс-акаунта з файлу-ключа.

        Потрібна для повідомлення про помилку: саме цією поштою треба
        поділитися таблицею, і людині зручніше побачити її одразу, ніж
        шукати всередині JSON-файлу.
        """
        try:
            data = json.loads(self._credentials_file.read_text(encoding="utf-8"))
            return str(data.get("client_email", "")) or "(не вказано у файлі-ключі)"
        except Exception:
            return "(не вдалося прочитати файл-ключ)"

    def _access_error(self, exc: Exception) -> SheetsError:
        """Найчастіша помилка проєкту — таблицею не поділилися з ботом.

        Google відповідає «403 The caller does not have permission», а
        gspread перетворює це на голий PermissionError без пояснень.
        Тому текст пишемо самі — з конкретною поштою, яку треба додати.
        """
        return SheetsError(
            "Google не дав доступ до таблиці (помилка 403).\n\n"
            "Найімовірніша причина: таблицею ще не поділилися з ботом.\n\n"
            "Що зробити:\n"
            "1. Відкрийте таблицю в Google Таблицях.\n"
            "2. Натисніть «Поділитися» (Share).\n"
            f"3. Додайте цю пошту: {self.service_account_email}\n"
            "4. Виберіть рівень доступу «Переглядач» (Viewer) і збережіть.\n\n"
            "Друга можлива причина — у .env вказано неправильний "
            "GOOGLE_SPREADSHEET_ID."
        )

    def _open_worksheet(self, sheet_name: str) -> gspread.Worksheet:
        """Відкриває конкретний аркуш таблиці."""
        client = self._connect()
        try:
            spreadsheet = client.open_by_key(self._spreadsheet_id)
        except PermissionError as exc:
            # gspread 6 кидає саме PermissionError замість APIError, коли
            # Google відповідає 403. Без цієї гілки користувач побачив би
            # порожнє «PermissionError» і не зрозумів би, що робити.
            raise self._access_error(exc) from exc
        except gspread.exceptions.APIError as exc:
            if "PERMISSION_DENIED" in str(exc) or "403" in str(exc):
                raise self._access_error(exc) from exc
            if _is_transient(exc):
                raise  # 5xx / 429 — тимчасова, хай цикл повтору спробує ще раз
            raise SheetsError(f"Google повернув помилку: {exc}") from exc
        except Exception as exc:
            if _is_transient(exc):
                raise  # мережевий збій — сирою нагору, до циклу повтору
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
        """Читає дозволені колонки одного розділу, з повтором при збоях мережі.

        Мережеві помилки (розрив з'єднання, таймаут, 5xx) повторюються до
        кількох разів із наростаючою паузою. Постійні помилки (403, немає
        аркуша чи колонки) не повторюються — від повтору вони не зникнуть,
        і користувач має одразу побачити зрозумілу причину.

        Порожній аркуш — це не помилка, а порожній список.
        """
        if not section.reads_data:
            return []

        last_exc: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._read_section_once(section)
            except SheetsError:
                raise  # постійна помилка — повтор не допоможе, показуємо одразу
            except Exception as exc:
                # Сюди доходять лише мережеві збої: _read_section_once усе
                # інше вже загорнув у SheetsError. Але про всяк випадок ще раз
                # перевіряємо — і не-мережеве не повторюємо.
                if not _is_transient(exc):
                    raise SheetsError(
                        f"Несподівана помилка читання «{section.sheet}»: {_brief(exc)}"
                    ) from exc
                last_exc = exc
                if attempt < self._max_attempts:
                    delay = self._backoffs[min(attempt - 1, len(self._backoffs) - 1)]
                    logger.warning(
                        "Читання «%s»: спроба %d з %d не вдалася (%s); повтор через %.0f с",
                        section.sheet,
                        attempt,
                        self._max_attempts,
                        _brief(exc),
                        delay,
                    )
                    self._sleep(delay)
                else:
                    logger.error(
                        "Читання «%s»: усі %d спроби не вдалися через мережу (%s)",
                        section.sheet,
                        self._max_attempts,
                        _brief(exc),
                    )

        raise SheetsError(
            "Не вдалося прочитати дані з таблиці: мережеве з'єднання нестабільне.\n"
            f"Зроблено {self._max_attempts} спроби, остання помилка: {_brief(last_exc)}."
        ) from last_exc

    def read_domain_list(self, sheet_name: str, header: str = "Domain") -> list[str]:
        """Читає одну службову колонку доменів без підключення її як бази.

        Аркуш вітрини має заголовок ``Domain`` у першому рядку; самі домени
        починаються з другого. Інші колонки фізично не запитуються.
        """
        worksheet = self._open_worksheet(sheet_name)
        try:
            headers = worksheet.row_values(1)
            position = _match_header(headers, header)
            if position is None:
                raise SheetsError(f"На аркуші «{sheet_name}» немає колонки «{header}».")
            letter = _column_letter(position)
            blocks = worksheet.batch_get([f"{letter}2:{letter}"], major_dimension="COLUMNS")
        except SheetsError:
            raise
        except Exception as exc:
            if _is_transient(exc):
                raise
            raise SheetsError(
                f"Не вдалося прочитати стоп-лист «{sheet_name}»: {type(exc).__name__}: {exc}"
            ) from exc
        return list(blocks[0][0]) if blocks and blocks[0] else []

    def _read_section_once(self, section: SectionConfig) -> list[dict[str, str]]:
        """Одна спроба прочитати розділ. Мережеві збої летять сирими нагору —
        їх ловить і повторює read_section."""
        worksheet = self._open_worksheet(section.sheet)

        # Крок 1: заголовки. Один невеликий запит.
        try:
            headers = worksheet.row_values(1)
        except Exception as exc:
            if _is_transient(exc):
                raise
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
            if _is_transient(exc):
                raise
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
