"""Спільні сервіси бота і журнал дій.

BotServices — це «валіза» з усім, що потрібно обробникам: налаштування,
карта колонок, сховище донорів, журнал. Замість глобальних змінних вона
передається явно — так одразу видно, хто чим користується, і так набагато
легше писати тести.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from app.bot.access import AccessStore
from app.data.columns import ColumnsConfig
from app.data.repository import DonorRepository
from app.llm.service import AIService
from app.settings import Settings


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Один запис у журналі дій."""

    at: float
    user_id: int
    action: str

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))


class ActionLog:
    """Журнал останніх дій — для адмін-меню.

    ВАЖЛИВО: сюди пишуться тільки службові події й агреговані числа
    («Меджик: Німеччина — 6 донорів»). Домени не потрапляють у журнал
    ніколи. Це не лише правило, а й наслідок будови: обробники отримують
    від аналітики самі числа, доменів у них просто немає.

    Журнал живе в пам'яті й обмежений за розміром: щойно записів стає більше
    за ліміт, найстаріші витісняються самі.
    """

    def __init__(self, capacity: int = 50) -> None:
        self._records: deque[ActionRecord] = deque(maxlen=capacity)

    def add(self, user_id: int, action: str) -> None:
        self._records.append(ActionRecord(at=time.time(), user_id=user_id, action=action))

    def recent(self, limit: int = 20) -> list[ActionRecord]:
        """Останні дії, найновіші зверху."""
        return list(self._records)[-limit:][::-1]

    def __len__(self) -> int:
        return len(self._records)


@dataclass(slots=True)
class BotServices:
    """Усе, що потрібно обробникам для роботи."""

    settings: Settings
    columns: ColumnsConfig
    repository: DonorRepository
    action_log: ActionLog = field(default_factory=ActionLog)
    ai: AIService | None = None
    """Сервіс ШІ для розмитих запитів. None — ШІ вимкнено (немає ключа)."""

    access_store: AccessStore | None = None
    """Динамічний список доступу (хто зайшов за КОДОМ). None — сховище не
    підключено (тоді діє лише статичний список ID з .env)."""

    def section_title(self, section_key: str) -> str:
        """Назва розділу для людини: "magic" → «Меджик»."""
        try:
            return self.columns.section(section_key).title
        except Exception:
            return section_key
