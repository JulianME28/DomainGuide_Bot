"""Доступ за кодом — Фаза 2: шлюз AccessMiddleware.

Перевіряємо саме АВТОРИЗАЦІЮ (не дані):
  * авторизовані (статичний .env АБО сховище) проходять;
  * код вимкнено → сторонній мовчки не проходить (регресія — як було);
  * код увімкнено → правильний = грант назавжди, невірний = відмова + спроба;
  * антибрутфорс: після N невдач — блок;
  * обрізка країв і регістр (той самий verify_code) працюють крізь шлюз;
  * код НІКОЛИ не з'являється в текстах шлюзу.
"""

from __future__ import annotations

import app.bot.middlewares as mw
from app.bot.access import AccessStore
from app.bot.context import BotServices
from app.bot.middlewares import (
    ACCESS_GRANTED_TEXT,
    ACCESS_PROMPT_TEXT,
    ACCESS_TOO_MANY_TEXT,
    ACCESS_WRONG_TEXT,
    AccessMiddleware,
)
from app.settings import Settings

CODE = "Team2026"


def make_settings(*, code: str = CODE, attempts: int = 5) -> Settings:
    return Settings(
        bot_token="t",
        data_backend="sheets",
        spreadsheet_id="s",
        credentials_file="c",
        allowed_user_ids=frozenset({100}),  # статичний користувач
        admin_user_ids=frozenset({200}),  # статичний адмін
        llm_provider="none",
        cache_ttl_seconds=1800,
        rate_limit_requests=20,
        rate_limit_window_seconds=60,
        log_level="INFO",
        access_code=code,
        access_code_attempts=attempts,
        access_code_window_seconds=3600,
    )


def make_services(tmp_path, *, code: str = CODE, attempts: int = 5) -> BotServices:
    store = AccessStore(tmp_path / "allowed.json")
    return BotServices(
        settings=make_settings(code=code, attempts=attempts),
        columns=None,
        repository=None,
        access_store=store,
    )


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _Event:
    """Мінімальна подія — шлюзу для allowed-гілки байдужий тип."""


async def _passes(mwi: AccessMiddleware, user_id: int) -> bool:
    """Чи пропустив шлюз користувача до обробника (True — викликав handler)."""
    called = {"v": False}

    async def handler(event, data):
        called["v"] = True

    await mwi(handler, _Event(), {"event_from_user": FakeUser(user_id)})
    return called["v"]


class TestШлюзПропускає:
    async def test_статичний_користувач_проходить(self, tmp_path):
        mwi = AccessMiddleware(make_services(tmp_path))
        assert await _passes(mwi, 100) is True

    async def test_статичний_адмін_проходить(self, tmp_path):
        mwi = AccessMiddleware(make_services(tmp_path))
        assert await _passes(mwi, 200) is True

    async def test_гранований_за_кодом_проходить(self, tmp_path):
        services = make_services(tmp_path)
        await services.access_store.grant(555)
        mwi = AccessMiddleware(services)
        assert await _passes(mwi, 555) is True

    async def test_сторонній_не_проходить(self, tmp_path):
        mwi = AccessMiddleware(make_services(tmp_path))
        assert await _passes(mwi, 999) is False


class TestКодВимкнено:
    async def test_регресія_сторонній_мовчки_не_проходить(self, tmp_path):
        # ACCESS_CODE порожній → давня поведінка (мовчання), сховище не діє.
        services = make_services(tmp_path, code="")
        mwi = AccessMiddleware(services)
        assert await _passes(mwi, 999) is False

    async def test_статичні_і_далі_працюють(self, tmp_path):
        services = make_services(tmp_path, code="")
        mwi = AccessMiddleware(services)
        assert await _passes(mwi, 100) is True
        assert await _passes(mwi, 200) is True


class TestУведенняКоду:
    async def test_правильний_код_грант_назавжди(self, tmp_path):
        services = make_services(tmp_path)
        mwi = AccessMiddleware(services)
        outcome = await mwi._process_text(555, CODE)
        assert outcome == "grant"
        assert services.access_store.contains(555)
        # Наступного разу вже проходить звичайним шляхом.
        assert await _passes(mwi, 555) is True

    async def test_невірний_код_відмова_без_гранту(self, tmp_path):
        services = make_services(tmp_path)
        mwi = AccessMiddleware(services)
        assert await mwi._process_text(555, "wrong") == "wrong"
        assert not services.access_store.contains(555)

    async def test_старт_дає_підказку_без_спроби(self, tmp_path):
        mwi = AccessMiddleware(make_services(tmp_path, attempts=2))
        assert await mwi._process_text(555, "/start") == "prompt"
        assert await mwi._process_text(555, "") == "prompt"
        # Дві підказки не з'їли ліміт спроб — код усе ще можна ввести.
        assert await mwi._process_text(555, CODE) == "grant"

    async def test_обрізка_країв_крізь_шлюз(self, tmp_path):
        services = make_services(tmp_path)
        mwi = AccessMiddleware(services)
        assert await mwi._process_text(555, "  Team2026 ") == "grant"
        assert services.access_store.contains(555)

    async def test_регістр_значущий_крізь_шлюз(self, tmp_path):
        mwi = AccessMiddleware(make_services(tmp_path))
        assert await mwi._process_text(555, "team2026") == "wrong"


class TestАнтибрутфорс:
    async def test_блок_після_N_невдач(self, tmp_path):
        mwi = AccessMiddleware(make_services(tmp_path, attempts=3))
        for _ in range(3):
            assert await mwi._process_text(555, "wrong") == "wrong"
        # Наступна спроба — заблоковано, навіть якщо код правильний.
        assert await mwi._process_text(555, CODE) == "blocked"

    async def test_правильний_код_скидає_лічильник(self, tmp_path):
        services = make_services(tmp_path, attempts=3)
        mwi = AccessMiddleware(services)
        await mwi._process_text(555, "wrong")
        await mwi._process_text(555, "wrong")
        assert await mwi._process_text(555, CODE) == "grant"  # скинуло до блоку
        assert services.access_store.contains(555)


class TestКодНеВТекстах:
    def test_код_не_світиться_в_підказках(self):
        for text in (
            ACCESS_PROMPT_TEXT,
            ACCESS_GRANTED_TEXT,
            ACCESS_WRONG_TEXT,
            ACCESS_TOO_MANY_TEXT,
        ):
            assert CODE not in text

    async def test_process_text_не_повертає_сам_код(self, tmp_path):
        # Санітарна перевірка: рішення шлюзу — це мітка, не сам код.
        mwi = AccessMiddleware(make_services(tmp_path))
        assert await mwi._process_text(555, CODE) in {"grant", "prompt", "wrong", "blocked"}


def test_модуль_не_тримає_код_у_памʼяті_глобально():
    # Захисна дрібниця: у модулі шлюзу немає глобальної змінної з кодом.
    assert not any(getattr(mw, name, None) == CODE for name in dir(mw))
