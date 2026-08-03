"""Доступ за кодом — Фаза 3: адмін бачить список за кодом і відкликає доступ.

Важливо на випадок витоку коду: адмін може вигнати зайвого, не міняючи код усім.
Відкликається ЛИШЕ динамічний доступ (за кодом); статичний .env не чіпається.
"""

from __future__ import annotations

from app.bot.access import AccessStore
from app.bot.context import BotServices
from app.bot.handlers.admin import admin_actions, cmd_revoke
from app.settings import Settings

ADMIN = 200
USER = 100


def make_settings(*, code: str = "Team2026") -> Settings:
    return Settings(
        bot_token="t",
        data_backend="sheets",
        spreadsheet_id="s",
        credentials_file="c",
        allowed_user_ids=frozenset({USER}),
        admin_user_ids=frozenset({ADMIN}),
        llm_provider="none",
        cache_ttl_seconds=1800,
        rate_limit_requests=20,
        rate_limit_window_seconds=60,
        log_level="INFO",
        access_code=code,
    )


async def make_services(tmp_path, *, code: str = "Team2026", granted=()) -> BotServices:
    store = AccessStore(tmp_path / "allowed.json")
    for uid in granted:
        await store.grant(uid)
    return BotServices(
        settings=make_settings(code=code),
        columns=None,
        repository=None,
        access_store=store,
    )


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self, text: str, user_id: int) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)


class FakeCbMessage:
    def __init__(self) -> None:
        self.edits: list[tuple] = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeCbMessage()
        self.answers: list[str] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)


class TestСписокЗаКодом:
    async def test_показує_код_користувачів(self, tmp_path):
        from app.bot.handlers.admin import _users_text

        services = await make_services(tmp_path, granted=(555, 777))
        text = _users_text(services)
        assert "За кодом" in text
        assert "555" in text and "777" in text
        assert "/revoke" in text  # підказка як прибрати

    async def test_порожній_список_за_кодом(self, tmp_path):
        from app.bot.handlers.admin import _users_text

        services = await make_services(tmp_path)
        text = _users_text(services)
        assert "поки нікого" in text

    async def test_код_вимкнено_показує_це(self, tmp_path):
        from app.bot.handlers.admin import _users_text

        services = await make_services(tmp_path, code="")
        text = _users_text(services)
        assert "вимкнено" in text.lower()

    async def test_статичні_id_видно(self, tmp_path):
        from app.bot.handlers.admin import _users_text

        services = await make_services(tmp_path)
        text = _users_text(services)
        assert str(ADMIN) in text and str(USER) in text


class TestВідкликанняКомандою:
    async def test_адмін_відкликає(self, tmp_path):
        services = await make_services(tmp_path, granted=(555,))
        message = FakeMessage("/revoke 555", ADMIN)
        await cmd_revoke(message, services)
        assert not services.access_store.contains(555)
        assert any("відкликано" in a for a in message.answers)

    async def test_не_адмін_не_може(self, tmp_path):
        services = await make_services(tmp_path, granted=(555,))
        message = FakeMessage("/revoke 555", USER)  # звичайний користувач
        await cmd_revoke(message, services)
        assert services.access_store.contains(555)  # доступ не чіпнули

    async def test_без_id_підказка(self, tmp_path):
        services = await make_services(tmp_path, granted=(555,))
        message = FakeMessage("/revoke", ADMIN)
        await cmd_revoke(message, services)
        assert services.access_store.contains(555)
        assert any("/revoke" in a for a in message.answers)

    async def test_невідомий_id(self, tmp_path):
        services = await make_services(tmp_path, granted=(555,))
        message = FakeMessage("/revoke 999", ADMIN)
        await cmd_revoke(message, services)
        assert any("немає в списку" in a for a in message.answers)


class TestВідкликанняКнопкою:
    async def test_кнопка_revoke_прибирає(self, tmp_path):
        services = await make_services(tmp_path, granted=(555, 777))
        callback = FakeCallback("admin:revoke:555", ADMIN)
        await admin_actions(callback, services)
        assert not services.access_store.contains(555)
        assert services.access_store.contains(777)  # інші не зачеплені
        # Екран перемальовано (edit_text викликано).
        assert callback.message.edits

    async def test_не_адмін_кнопкою_не_може(self, tmp_path):
        services = await make_services(tmp_path, granted=(555,))
        callback = FakeCallback("admin:revoke:555", USER)
        await admin_actions(callback, services)
        assert services.access_store.contains(555)

    async def test_некоректний_id_кнопкою(self, tmp_path):
        services = await make_services(tmp_path, granted=(555,))
        callback = FakeCallback("admin:revoke:abc", ADMIN)
        await admin_actions(callback, services)
        assert services.access_store.contains(555)  # нічого не зламали
