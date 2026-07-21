"""Стійкість до мережевих збоїв при читанні Google Sheets.

Реальна проблема з роботи: Google інколи рве з'єднання
(ConnectionResetError / WinError 10054). Бот не крашився, але казав «база
тимчасово недоступна», і користувач лишався без відповіді.

Тут перевіряється три речі:

  1. КЛАСИФІКАЦІЯ. Мережеві/тимчасові помилки (розрив, таймаут, 5xx)
     позначаються як такі, що варто повторити. 403, відсутній аркуш чи
     колонка — ні: від повтору вони не зникнуть.

  2. ПОВТОР. Мережева помилка з наступним успіхом дає результат без
     помилки; три поспіль невдачі здаються з зрозумілою помилкою; постійні
     помилки не повторюються взагалі.

  3. ВІДДАЧА КЕШУ. Якщо оновлення не вдалося, але в пам'яті є попередні
     дані — репозиторій віддає їх із поміткою про час, а не «недоступна».
     Якщо кешу немає (перший запуск) — тоді чесна помилка.
"""

from __future__ import annotations

import gspread
import pytest
import requests

from app.analytics.engine import run_query
from app.analytics.query import DonorQuery
from app.data.models import Dataset
from app.data.repository import DonorRepository, build_donors
from app.data.sheets import SheetsError, SheetsReader, _is_transient
from app.text.cards import render_result
from tests.fixtures.fake_data import FakeReader, magic_rows


class FakeResponse:
    """Мінімальна відповідь для конструктора gspread.APIError."""

    def __init__(self, status: int) -> None:
        self.status_code = status
        self.text = f"HTTP {status}"

    def json(self) -> dict:
        return {"error": {"code": self.status_code, "message": "збій", "status": "ERR"}}


def api_error(status: int) -> gspread.exceptions.APIError:
    return gspread.exceptions.APIError(FakeResponse(status))


def make_reader(sleeps: list[float], **kw) -> SheetsReader:
    """Читач із миттєвим «сном» — тести не чекають справжніх секунд."""
    return SheetsReader("id", "credentials.json", sleeper=sleeps.append, **kw)


# ---------------------------------------------------------------------------
# 1. Класифікація помилок
# ---------------------------------------------------------------------------


class TestКласифікаціяПомилок:
    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionResetError(10054, "Віддалений хост примусово розірвав з'єднання"),
            ConnectionError("reset"),
            ConnectionAbortedError("aborted"),
            TimeoutError("timed out"),
            requests.exceptions.ConnectionError("conn drop"),
            requests.exceptions.Timeout("slow"),
            requests.exceptions.ChunkedEncodingError("truncated"),
        ],
    )
    def test_мережеві_повторюємо(self, exc):
        assert _is_transient(exc)

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_5xx_і_429_повторюємо(self, status):
        assert _is_transient(api_error(status))

    @pytest.mark.parametrize("status", [400, 403, 404])
    def test_4xx_доступу_не_повторюємо(self, status):
        assert not _is_transient(api_error(status))

    @pytest.mark.parametrize(
        "exc",
        [PermissionError("403"), ValueError("щось"), KeyError("k"), SheetsError("немає колонки")],
    )
    def test_постійні_не_повторюємо(self, exc):
        assert not _is_transient(exc)

    def test_розрив_усередині_ланцюжка_причин(self):
        """Requests часто ховає мережевий збій під кількома обгортками."""
        try:
            try:
                raise ConnectionResetError(10054, "розрив")
            except ConnectionResetError as inner:
                raise RuntimeError("обгортка") from inner
        except RuntimeError as outer:
            assert _is_transient(outer)

    def test_текстова_ознака_10054(self):
        """Навіть якщо тип незнайомий, текст про 10054 видає мережевий збій."""
        assert _is_transient(OSError("[WinError 10054] connection reset by peer"))


# ---------------------------------------------------------------------------
# 2. Повтор спроб у SheetsReader
# ---------------------------------------------------------------------------


class TestПовторСпроб:
    def test_мережева_помилка_потім_успіх(self, columns_config):
        sleeps: list[float] = []
        reader = make_reader(sleeps)
        section = columns_config.section("magic")

        attempts = {"n": 0}

        def flaky(_section):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionResetError(10054, "розрив")
            return [{"domain": "ok.de"}]

        reader._read_section_once = flaky

        rows = reader.read_section(section)

        assert rows == [{"domain": "ok.de"}], "друга спроба вдалася — результат без помилки"
        assert attempts["n"] == 2
        assert sleeps == [1.0], "перед повтором була одна пауза 1 с"

    def test_три_поспіль_невдачі_здаються(self, columns_config):
        sleeps: list[float] = []
        reader = make_reader(sleeps)
        attempts = {"n": 0}

        def always_fail(_section):
            attempts["n"] += 1
            raise ConnectionResetError(10054, "розрив")

        reader._read_section_once = always_fail

        with pytest.raises(SheetsError, match="нестабільне"):
            reader.read_section(columns_config.section("magic"))

        assert attempts["n"] == 3, "рівно 3 спроби"
        assert sleeps == [1.0, 3.0], "паузи наростають: 1 с, потім 3 с"

    def test_успіх_з_першого_разу_без_пауз(self, columns_config):
        sleeps: list[float] = []
        reader = make_reader(sleeps)
        reader._read_section_once = lambda _s: [{"domain": "ok.de"}]

        assert reader.read_section(columns_config.section("magic")) == [{"domain": "ok.de"}]
        assert sleeps == []

    def test_403_не_повторюється(self, columns_config):
        """Постійну помилку показуємо одразу, без повторів."""
        sleeps: list[float] = []
        reader = make_reader(sleeps)
        attempts = {"n": 0}

        def permission_denied(_section):
            attempts["n"] += 1
            raise reader._access_error(PermissionError())

        reader._read_section_once = permission_denied

        with pytest.raises(SheetsError) as info:
            reader.read_section(columns_config.section("magic"))

        assert attempts["n"] == 1, "жодного повтору"
        assert sleeps == []
        assert "403" in str(info.value)
        assert "Поділитися" in str(info.value)

    def test_відсутня_колонка_не_повторюється(self, columns_config):
        """Немає колонки — реальний шлях розбору заголовків, без повтору."""
        sleeps: list[float] = []
        reader = make_reader(sleeps)

        class FakeWorksheet:
            def row_values(self, _n):
                return ["Domain", "Мова", "DR"]  # немає Traffic

        reader._open_worksheet = lambda _name: FakeWorksheet()

        with pytest.raises(SheetsError, match="немає колонки"):
            reader.read_section(columns_config.section("magic"))

        assert sleeps == [], "постійну помилку не повторюємо"

    def test_403_через_реальне_відкриття_не_повторюється(self, columns_config):
        """Наскрізно: 403 від gspread → зрозуміла інструкція, без повторів."""
        sleeps: list[float] = []
        reader = make_reader(sleeps)

        class FakeClient:
            def open_by_key(self, _key):
                raise PermissionError

        reader._connect = lambda: FakeClient()

        with pytest.raises(SheetsError) as info:
            reader.read_section(columns_config.section("magic"))

        assert sleeps == []
        assert "Поділитися" in str(info.value)

    def test_невідома_нетимчасова_помилка_не_повторюється(self, columns_config):
        sleeps: list[float] = []
        reader = make_reader(sleeps)
        attempts = {"n": 0}

        def boom(_section):
            attempts["n"] += 1
            raise ValueError("несподіванка")

        reader._read_section_once = boom

        with pytest.raises(SheetsError):
            reader.read_section(columns_config.section("magic"))

        assert attempts["n"] == 1
        assert sleeps == []


# ---------------------------------------------------------------------------
# 3. Віддача кешу при невдалому оновленні
# ---------------------------------------------------------------------------


class TestВіддачаКешу:
    async def test_невдале_оновлення_віддає_кеш_із_поміткою(self, columns_config):
        reader = FakeReader({"magic": magic_rows()})
        # ttl=0 → кожен запит намагається оновитися заново.
        repo = DonorRepository(reader, columns_config, ttl_seconds=0)

        first = await repo.get("magic")
        assert first.available and not first.stale

        # Мережа впала.
        reader.fail_with("magic", ConnectionResetError(10054, "розрив"))
        second = await repo.get("magic")

        assert second.available, "не «недоступна» — віддали кеш"
        assert second.stale, "але позначено як застаріле"
        assert second.count == first.count, "ті самі числа"
        assert second.loaded_at == first.loaded_at, "час першого успішного оновлення"

    async def test_кеш_не_псується_при_збої(self, columns_config):
        reader = FakeReader({"magic": magic_rows()})
        repo = DonorRepository(reader, columns_config, ttl_seconds=0)
        await repo.get("magic")

        reader.fail_with("magic", ConnectionResetError())
        await repo.get("magic")

        # У самому кеші лишилися добрі дані — наступний запит спробує ще раз.
        assert repo.peek("magic").available
        assert not repo.peek("magic").stale

    async def test_після_відновлення_мережі_дані_свіжі(self, columns_config):
        reader = FakeReader({"magic": magic_rows()})
        repo = DonorRepository(reader, columns_config, ttl_seconds=0)
        await repo.get("magic")

        reader.fail_with("magic", ConnectionResetError())
        assert (await repo.get("magic")).stale

        reader.recover("magic")
        recovered = await repo.get("magic")
        assert recovered.available and not recovered.stale

    async def test_перший_запуск_без_кешу_дає_помилку(self, columns_config):
        """Кешу ще немає — тоді як раніше: зрозуміла помилка, не застаріле."""
        reader = FakeReader({"magic": magic_rows()})
        reader.fail_with("magic", ConnectionResetError(10054, "розрив"))
        repo = DonorRepository(reader, columns_config)

        dataset = await repo.get("magic")

        assert not dataset.available
        assert not dataset.stale
        assert dataset.error

    async def test_непридатний_кеш_не_вважається_даними(self, columns_config):
        """Якщо в кеші лежить попередня ПОМИЛКА (не дані) — застаріле не віддаємо."""
        reader = FakeReader({"magic": magic_rows()})
        repo = DonorRepository(reader, columns_config)

        reader.fail_with("magic", ConnectionResetError())
        await repo.get("magic")  # кешується недоступність
        second = await repo.get("magic")

        assert not second.available
        assert not second.stale


# ---------------------------------------------------------------------------
# 4. Помітка в картці
# ---------------------------------------------------------------------------


class TestПоміткаВКартці:
    def _stale_dataset(self) -> Dataset:
        donors, _ = build_donors(magic_rows())
        # loaded_at у минулому — фіксований, щоб перевірити текст часу.
        return Dataset(
            section_key="magic",
            title="Меджик",
            sheet_name="Меджик",
            donors=donors,
            loaded_at=1_700_000_000.0,
            available=True,
            stale=True,
        )

    def test_застаріла_картка_має_помітку(self):
        result = run_query(self._stale_dataset(), DonorQuery(section_key="magic"))
        card = render_result(result)

        assert "Онлайн-оновлення зараз недоступне" in card
        assert "станом на" in card
        assert result.stale

    async def test_свіжа_картка_без_помітки(self, magic):
        from app.dictionary.countries import country_by_code

        card = render_result(
            run_query(magic, DonorQuery(section_key="magic", country=country_by_code("de")))
        )
        assert "Онлайн-оновлення" not in card
        assert "станом на" not in card
