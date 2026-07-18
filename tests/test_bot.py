"""Тести телеграм-частини: доступ, ліміт, клавіатури, стани, запуск."""

from __future__ import annotations

import pytest

from app.analytics.query import DonorQuery
from app.bot.context import ActionLog, BotServices
from app.bot.execution import execute
from app.bot.handlers import build_router
from app.bot.keyboards import (
    admin_menu,
    country_picker,
    main_menu,
    result_menu,
    section_menu,
    wizard_confirm,
    wizard_countries,
    wizard_dr,
    wizard_traffic,
)
from app.bot.middlewares import AccessMiddleware, RateLimitMiddleware
from app.bot.states import query_from_state, query_to_state, summary_lines
from app.dictionary.countries import country_by_code
from app.dictionary.languages import language_by_code
from app.settings import Settings


def make_settings(allowed=(1, 2), admins=(1,)) -> Settings:
    return Settings(
        bot_token="test",
        data_backend="sheets",
        spreadsheet_id="test",
        credentials_file="credentials.json",
        allowed_user_ids=frozenset(allowed),
        admin_user_ids=frozenset(admins),
        llm_provider="none",
        cache_ttl_seconds=1800,
        rate_limit_requests=3,
        rate_limit_window_seconds=60,
        log_level="INFO",
    )


@pytest.fixture
def services(repository, columns_config) -> BotServices:
    return BotServices(
        settings=make_settings(),
        columns=columns_config,
        repository=repository,
        action_log=ActionLog(),
    )


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class TestЗбіркаБота:
    def test_маршрутизатор_збирається_з_правильним_порядком(self):
        """Збираємо рівно один раз: aiogram не дозволяє підключити ті самі
        обробники до другого маршрутизатора, та й у бою це робиться однократно.

        Порядок критичний: «freeform» приймає будь-який текст, тому має бути
        останнім — інакше він перехоплював би відповіді на кроках майстра.
        """
        router = build_router()

        assert router.name == "root"
        names = [child.name for child in router.sub_routers]
        assert names[-1] == "freeform"
        assert "wizard" in names
        assert names.index("wizard") < names.index("freeform")


class TestКлавіатури:
    @pytest.mark.parametrize(
        "keyboard",
        [
            main_menu(),
            main_menu(is_admin=True),
            section_menu("magic"),
            country_picker("magic"),
            result_menu("magic"),
            wizard_countries(),
            wizard_traffic(),
            wizard_dr(),
            wizard_confirm(),
            admin_menu(),
        ],
    )
    def test_callback_data_вкладається_в_ліміт(self, keyboard):
        """Telegram дозволяє не більше 64 байтів на кнопку."""
        for row in keyboard.inline_keyboard:
            for button in row:
                assert button.callback_data
                assert len(button.callback_data.encode("utf-8")) <= 64

    def test_адмін_бачить_додаткову_кнопку(self):
        plain = str(main_menu())
        with_admin = str(main_menu(is_admin=True))
        assert "admin:menu" not in plain
        assert "admin:menu" in with_admin

    def test_на_кожному_кроці_майстра_є_назад_і_скинути(self):
        """Вимога ТЗ, розділ 29."""
        for keyboard in (wizard_traffic(), wizard_dr(), wizard_confirm()):
            data = str(keyboard)
            assert "wizard:reset" in data, "має бути кнопка «Скинути»"
            assert "back" in data or "wizard:back" in data, "має бути кнопка «Назад»"

    def test_крок_називається_країна_а_не_гео(self):
        """У даних немає гео — обіцяти його в інтерфейсі не можна."""
        from app.bot.handlers.wizard import STEP_COUNTRY

        assert "КРАЇНУ" in STEP_COUNTRY
        assert "гео" not in STEP_COUNTRY.lower().replace("гео визначається", "")


class TestДоступ:
    async def test_чужого_не_пускають(self, services):
        middleware = AccessMiddleware(services)
        called = False

        async def handler(event, data):
            nonlocal called
            called = True

        await middleware(handler, object(), {"event_from_user": FakeUser(999)})
        assert not called, "стороннього не має бути пропущено далі"

    async def test_свого_пускають(self, services):
        middleware = AccessMiddleware(services)
        called = False

        async def handler(event, data):
            nonlocal called
            called = True

        await middleware(handler, object(), {"event_from_user": FakeUser(1)})
        assert called

    def test_адмін_має_і_звичайний_доступ(self):
        """Інакше вийшло б: адмін є, а користуватися ботом не може."""
        settings = make_settings(allowed=(2,), admins=(7,))
        assert settings.is_allowed(7)
        assert settings.is_admin(7)
        assert not settings.is_admin(2)

    def test_список_доступу_не_в_коді(self):
        """ID мають братися з .env, а не бути зашитими в код."""
        import inspect

        import app.bot.middlewares as middlewares

        source = inspect.getsource(middlewares)
        assert "850410806" not in source


class TestЛімітЗапитів:
    async def test_після_ліміту_запити_не_проходять(self):
        middleware = RateLimitMiddleware(limit=3, window_seconds=60)
        passed = 0

        async def handler(event, data):
            nonlocal passed
            passed += 1

        for _ in range(5):
            await middleware(handler, object(), {"event_from_user": FakeUser(1)})

        assert passed == 3, "перші три пройшли, решта — ні"

    async def test_ліміт_окремий_для_кожного(self):
        middleware = RateLimitMiddleware(limit=2, window_seconds=60)
        passed = []

        async def handler(event, data):
            passed.append(data["event_from_user"].id)

        for _ in range(3):
            await middleware(handler, object(), {"event_from_user": FakeUser(1)})
        await middleware(handler, object(), {"event_from_user": FakeUser(2)})

        assert passed.count(1) == 2
        assert passed.count(2) == 1


class TestЗберіганняЗапиту:
    def test_запит_зберігається_і_відновлюється(self):
        original = DonorQuery(
            section_key="magic",
            country=country_by_code("de"),
            language=language_by_code("en"),
            dr_min=30,
            traffic_min=100,
        )
        restored = query_from_state(query_to_state(original))

        assert restored.section_key == "magic"
        assert restored.country is country_by_code("de")
        assert restored.language is language_by_code("en")
        assert restored.dr_min == 30
        assert restored.traffic_min == 100

    def test_порожній_запит_теж_відновлюється(self):
        restored = query_from_state({})
        assert restored.section_key == "magic"
        assert restored.country is None

    def test_у_памʼяті_лише_прості_значення(self):
        """Стан зберігається як прості типи — інакше він не переживе перезапуск."""
        data = query_to_state(DonorQuery(section_key="magic", country=country_by_code("de")))
        for value in data.values():
            assert value is None or isinstance(value, str | int | float | list)

    def test_резюме_фільтрів(self):
        query = DonorQuery(section_key="magic", country=country_by_code("gb"), traffic_min=1)
        text = summary_lines(query, "Меджик")

        assert "Меджик" in text
        assert "Британія" in text
        assert "від 1" in text
        assert "Країна" in text, "крок називається «Країна», а не «Гео»"


class TestЖурналДій:
    def test_записує_і_віддає_найновіші_зверху(self):
        log = ActionLog(capacity=10)
        log.add(1, "перша")
        log.add(1, "друга")

        records = log.recent()
        assert records[0].action == "друга"

    def test_старі_записи_витісняються(self):
        log = ActionLog(capacity=3)
        for i in range(10):
            log.add(1, f"дія {i}")
        assert len(log) == 3


class TestВиконанняЗапиту:
    async def test_запит_виконується_наскрізно(self, services):
        executed = await execute(
            services, DonorQuery(section_key="magic", country=country_by_code("de"))
        )

        assert executed.result.core.count == 6
        assert executed.result.addendum.count == 4
        assert "Знайдено донорів" in executed.text

    async def test_у_тексті_немає_доменів(self, services, magic):
        """Наскрізна перевірка безпеки: від бази до готового повідомлення."""
        for code in ("de", "fr", "gb"):
            executed = await execute(
                services, DonorQuery(section_key="magic", country=country_by_code(code))
            )
            for donor in magic.donors:
                assert donor.domain not in executed.text

    async def test_у_журнал_не_потрапляють_домени(self, services, magic):
        from app.text.cards import render_summary

        executed = await execute(
            services, DonorQuery(section_key="magic", country=country_by_code("de"))
        )
        services.action_log.add(1, render_summary(executed.result))

        for record in services.action_log.recent():
            for donor in magic.donors:
                assert donor.domain not in record.action

    async def test_порожня_база_не_валить_виконання(self, services):
        executed = await execute(services, DonorQuery(section_key="mordy"))
        assert executed.result.core.count == 0


class TestСтартБезКолонкиГЕО:
    """Окрема вимога: бот має підніматися на даних без колонки країни."""

    def test_карта_колонок_не_містить_гео(self, columns_config):
        for section in columns_config.sections.values():
            assert "geo" not in section.columns

    async def test_бот_повністю_працює_без_гео(self, services):
        """Наскрізно: конфіг → дані → запит → картка, без жодного поля гео."""
        executed = await execute(
            services, DonorQuery(section_key="magic", country=country_by_code("de"))
        )

        assert executed.result.available
        assert executed.result.core.count > 0
        assert "Німеччина" in executed.text
        # Країна визначилась із доменної зони, а не з колонки.
        assert ".de" in executed.text

    def test_сервіси_збираються(self, services):
        assert services.section_title("magic") == "Меджик"
        assert services.section_title("невідомий") == "невідомий"
