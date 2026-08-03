"""Доступ за кодом — Фаза 1: сховище, звірка коду, антибрутфорс.

Межі безпеки даних не чіпаємо: тут лише авторизація. Головні гарантії:
  * код звіряється з обрізкою КРАЇВ, але регістр значущий;
  * файл грантів переживає перезапуск і не падає на побитому вмісті;
  * код НІКОЛИ не потрапляє в repr Settings (спільний сервер!).
"""

from __future__ import annotations

import json

from app.bot.access import AccessStore, AttemptLimiter, GrantedUser, verify_code


class TestVerifyCode:
    def test_точний_збіг(self):
        assert verify_code("Team2026", "Team2026") is True

    def test_обрізка_країв(self):
        # Люди копіюють код із хвостовим/початковим пробілом — це той самий код.
        assert verify_code("Team2026 ", "Team2026") is True
        assert verify_code("  Team2026  ", "Team2026") is True
        assert verify_code("Team2026", "Team2026 ") is True

    def test_регістр_значущий(self):
        assert verify_code("team2026", "Team2026") is False
        assert verify_code("TEAM2026", "Team2026") is False

    def test_внутрішні_пробіли_значущі(self):
        # Обрізаємо лише краї; пробіл усередині — частина коду.
        assert verify_code("Team 2026", "Team2026") is False

    def test_невірний_код(self):
        assert verify_code("wrong", "Team2026") is False

    def test_порожній_заданий_код_завжди_false(self):
        # Функція вимкнена (код не заданий) — жоден ввід не проходить.
        assert verify_code("будь-що", "") is False
        assert verify_code("", "") is False

    def test_не_ascii_код(self):
        # Порівняння на байтах — кирилиця теж працює.
        assert verify_code("Пароль7", "Пароль7") is True
        assert verify_code("пароль7", "Пароль7") is False


class TestAccessStore:
    async def test_грант_і_contains(self, tmp_path):
        store = AccessStore(tmp_path / "allowed.json")
        assert store.contains(111) is False
        await store.grant(111)
        assert store.contains(111) is True

    async def test_запис_на_диск_і_перечитування(self, tmp_path):
        path = tmp_path / "allowed.json"
        store = AccessStore(path)
        await store.grant(111)
        await store.grant(222, source="client-x")

        # Новий екземпляр читає той самий файл — грант пережив «перезапуск».
        again = AccessStore(path)
        again.load()
        assert again.contains(111) and again.contains(222)
        sources = {u.user_id: u.source for u in again.list()}
        assert sources == {111: "code", 222: "client-x"}

    async def test_файл_справді_json(self, tmp_path):
        path = tmp_path / "allowed.json"
        store = AccessStore(path)
        await store.grant(111)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert data[0]["user_id"] == 111

    async def test_revoke(self, tmp_path):
        store = AccessStore(tmp_path / "allowed.json")
        await store.grant(111)
        assert await store.revoke(111) is True
        assert store.contains(111) is False
        # Повторний revoke — нікого немає, False.
        assert await store.revoke(111) is False

    async def test_revoke_переживає_перезапуск(self, tmp_path):
        path = tmp_path / "allowed.json"
        store = AccessStore(path)
        await store.grant(111)
        await store.grant(222)
        await store.revoke(111)

        again = AccessStore(path)
        again.load()
        assert not again.contains(111)
        assert again.contains(222)

    async def test_повторний_грант_не_дублює(self, tmp_path):
        store = AccessStore(tmp_path / "allowed.json")
        await store.grant(111)
        await store.grant(111)
        assert len(store.list()) == 1

    def test_відсутній_файл_порожньо_без_краху(self, tmp_path):
        store = AccessStore(tmp_path / "nope.json")
        store.load()  # не падає
        assert store.list() == []

    def test_побитий_файл_порожньо_без_краху(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{ це не валідний json", encoding="utf-8")
        store = AccessStore(path)
        store.load()  # warning у лог, але не падає
        assert store.list() == []

    def test_директорія_створюється(self, tmp_path):
        # data/ може ще не існувати — сховище створює її саме.
        store = AccessStore(tmp_path / "data" / "allowed.json")
        store.load()
        assert store.list() == []

    def test_list_відсортований_за_часом(self, tmp_path):
        store = AccessStore(tmp_path / "allowed.json")
        # Підкладаємо напряму з різним часом (без async — перевіряємо сортування).
        store._users = {
            2: GrantedUser(2, granted_at=200.0),
            1: GrantedUser(1, granted_at=100.0),
        }
        assert [u.user_id for u in store.list()] == [1, 2]


class TestAttemptLimiter:
    def test_у_межах_ліміту_не_блокує(self):
        limiter = AttemptLimiter(limit=3, window_seconds=3600, clock=lambda: 0.0)
        limiter.register_failure(1)
        limiter.register_failure(1)
        assert limiter.blocked(1) is False

    def test_перевищення_блокує(self):
        limiter = AttemptLimiter(limit=3, window_seconds=3600, clock=lambda: 0.0)
        for _ in range(3):
            limiter.register_failure(1)
        assert limiter.blocked(1) is True

    def test_вікно_звільняє(self):
        now = [0.0]
        limiter = AttemptLimiter(limit=2, window_seconds=100, clock=lambda: now[0])
        limiter.register_failure(1)
        limiter.register_failure(1)
        assert limiter.blocked(1) is True
        now[0] = 101.0  # найстаріші спроби вийшли за вікно
        assert limiter.blocked(1) is False

    def test_успіх_скидає(self):
        limiter = AttemptLimiter(limit=2, window_seconds=3600, clock=lambda: 0.0)
        limiter.register_failure(1)
        limiter.register_failure(1)
        assert limiter.blocked(1) is True
        limiter.reset(1)
        assert limiter.blocked(1) is False

    def test_окремі_користувачі_незалежні(self):
        limiter = AttemptLimiter(limit=1, window_seconds=3600, clock=lambda: 0.0)
        limiter.register_failure(1)
        assert limiter.blocked(1) is True
        assert limiter.blocked(2) is False


class TestКодНеВРепрі:
    """Спільний сервер: код НЕ має світитися в repr Settings чи логах."""

    def test_access_code_не_в_repr(self):
        from app.settings import Settings

        settings = Settings(
            bot_token="secret-token",
            data_backend="sheets",
            spreadsheet_id="s",
            credentials_file="c",
            allowed_user_ids=frozenset({1}),
            admin_user_ids=frozenset(),
            llm_provider="none",
            cache_ttl_seconds=1800,
            rate_limit_requests=20,
            rate_limit_window_seconds=60,
            log_level="INFO",
            access_code="Team2026",
        )
        text = repr(settings)
        assert "Team2026" not in text  # код прихований
        assert "secret-token" not in text  # і токен, для певності
        # А функція-прапорець працює:
        assert settings.access_code_enabled is True
