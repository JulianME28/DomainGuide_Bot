"""Доступ за КОДОМ: динамічний список тих, хто зайшов, не через ручний .env.

Наявний список ID у .env — статичний (правиться руками). Тут — ДРУГИЙ,
динамічний список: коли нова людина вводить правильний код доступу, її
Telegram ID дописується у файл на диску й запам'ятовується назавжди.

Три складові:
  * verify_code   — звірка введеного коду із заданим (обрізка країв, регістр
                    значущий, константне за часом порівняння);
  * AccessStore   — файлове сховище гранованих ID (переживає перезапуск і
                    git pull, у git не потрапляє — файл .json уже в .gitignore);
  * AttemptLimiter — антибрутфорс: ковзне вікно НЕВДАЛИХ спроб на користувача.

Межі безпеки ДАНИХ не змінюються: це лише авторизація (хто взагалі проходить
шлюз). Сам код — секрет у .env, у лог і в тексти відмов не потрапляє НІКОЛИ.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger(__name__)


def verify_code(candidate: str, code: str) -> bool:
    """Чи збігається введений код із заданим.

    Пробіли по КРАЯХ обрізаємо (люди копіюють код із хвостовим пробілом), але
    РЕГІСТР значущий: «Team2026 » == «Team2026», а «team2026» ≠ «Team2026» — код
    навмисно сильний. Порівняння константне за часом (hmac.compare_digest на
    байтах, щоб працювало й для не-ASCII), аби таймінг не підказував код.
    Порожній заданий код завжди дає False (функція вимкнена)."""
    if not code:
        return False
    return hmac.compare_digest(candidate.strip().encode("utf-8"), code.strip().encode("utf-8"))


@dataclass(frozen=True, slots=True)
class GrantedUser:
    """Один запис у динамічному списку доступу."""

    user_id: int
    granted_at: float
    source: str = "code"
    """Звідки доступ. Поки завжди "code"; заділ під окремі коди клієнтів —
    тоді сюди піде назва клієнта, а сховище й адмінка вже вміють це показати."""

    @property
    def granted_text(self) -> str:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(self.granted_at))


class AccessStore:
    """Список тих, хто зайшов за кодом — на диску.

    Файл JSON: [{"user_id":.., "granted_at":.., "source":..}]. Запис
    АТОМАРНИЙ (тимчасовий файл + os.replace), щоб краш під час запису не
    побив список. Побитий або відсутній файл на старті → вважаємо порожнім і
    пишемо warning, але НЕ падаємо. Записи рідкі (лише грант/відкликання), тож
    прості синхронні записи під asyncio.Lock — цього досить (бот — один процес).
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._users: dict[int, GrantedUser] = {}
        self._lock = asyncio.Lock()

    def load(self) -> None:
        """Читає файл у пам'ять. Викликається один раз на старті бота."""
        if not self._path.exists():
            logger.info("Файл доступу за кодом ще не створено (%s) — список порожній", self._path)
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "Не вдалося прочитати файл доступу %s: %s — вважаю список порожнім",
                self._path,
                exc,
            )
            return

        users: dict[int, GrantedUser] = {}
        for item in raw if isinstance(raw, list) else ():
            if not isinstance(item, dict):
                continue
            try:
                uid = int(item["user_id"])
            except (KeyError, TypeError, ValueError):
                continue
            users[uid] = GrantedUser(
                user_id=uid,
                granted_at=_as_float(item.get("granted_at")),
                source=str(item.get("source", "code")),
            )
        self._users = users
        logger.info("Список доступу за кодом: %d користувач(ів)", len(self._users))

    def contains(self, user_id: int) -> bool:
        """Чи має цей ID динамічний доступ (виданий за кодом)."""
        return user_id in self._users

    def list(self) -> list[GrantedUser]:
        """Усі гранти, від найстаріших до найновіших."""
        return sorted(self._users.values(), key=lambda user: user.granted_at)

    async def grant(self, user_id: int, source: str = "code") -> None:
        """Додає ID назавжди (write-through у файл). Повтор — тихо ігнорується."""
        async with self._lock:
            if user_id in self._users:
                return
            self._users[user_id] = GrantedUser(
                user_id=user_id, granted_at=time.time(), source=source
            )
            self._write()
        logger.info("Доступ за кодом надано: Telegram ID %s (джерело=%s)", user_id, source)

    async def revoke(self, user_id: int) -> bool:
        """Прибирає ID зі списку (write-through). True — був і прибрали."""
        async with self._lock:
            if user_id not in self._users:
                return False
            del self._users[user_id]
            self._write()
        logger.info("Доступ за кодом відкликано: Telegram ID %s", user_id)
        return True

    def _write(self) -> None:
        """Атомарний перезапис усього списку. Директорію створюємо за потреби."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {"user_id": user.user_id, "granted_at": user.granted_at, "source": user.source}
            for user in self.list()
        ]
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


class AttemptLimiter:
    """Ковзне вікно НЕВДАЛИХ спроб коду на користувача — антибрутфорс.

    Рахуємо лише НЕВДАЛІ спроби (успіх скидає лічильник). Перевищено ліміт —
    доти, доки найстаріша спроба не вийде за вікно, нові спроби не приймаємо."""

    def __init__(
        self, limit: int, window_seconds: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._fails: dict[int, deque[float]] = defaultdict(deque)

    def _prune(self, user_id: int, now: float) -> deque[float]:
        fails = self._fails[user_id]
        while fails and now - fails[0] > self._window:
            fails.popleft()
        return fails

    def blocked(self, user_id: int) -> bool:
        """Чи вичерпано ліміт саме зараз (нову спробу НЕ реєструє)."""
        return len(self._prune(user_id, self._clock())) >= self._limit

    def register_failure(self, user_id: int) -> None:
        """Фіксує одну невдалу спробу."""
        now = self._clock()
        self._prune(user_id, now)
        self._fails[user_id].append(now)

    def reset(self, user_id: int) -> None:
        """Успішний код — забуваємо попередні невдачі цього користувача."""
        self._fails.pop(user_id, None)


def _as_float(value: object) -> float:
    """М'яко приводить значення granted_at до float (0.0, якщо не вийшло)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "AccessStore",
    "AttemptLimiter",
    "GrantedUser",
    "verify_code",
]
