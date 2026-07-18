"""Тести читача Google Sheets.

Мережі тут немає: gspread підмінений. Перевіряємо логіку, а не Google.
"""

from __future__ import annotations

import json

import pytest

from app.data.sheets import SheetsError, SheetsReader, _column_letter, _match_header


class TestНомериКолонок:
    @pytest.mark.parametrize(
        ("index", "letter"),
        [(1, "A"), (2, "B"), (26, "Z"), (27, "AA"), (28, "AB"), (52, "AZ"), (53, "BA")],
    )
    def test_номер_перетворюється_на_літеру(self, index, letter):
        assert _column_letter(index) == letter


class TestПошукЗаголовка:
    def test_точний_збіг(self):
        assert _match_header(["Domain", "Мова", "DR"], "Мова") == 2

    def test_без_урахування_пробілів_і_регістру(self):
        """У заголовках таблиці теж бувають хвостові пробіли."""
        assert _match_header(["Domain ", " МОВА", "dr"], "Мова") == 2
        assert _match_header(["Domain ", " МОВА", "dr"], "DR") == 3

    def test_колонки_немає(self):
        assert _match_header(["Domain", "DR"], "Traffic") is None

    def test_точний_збіг_має_перевагу(self):
        """Якщо є і точний, і приблизний збіг — беремо точний."""
        assert _match_header(["мова", "Мова"], "Мова") == 2


class TestПоштаСервісАкаунта:
    def test_читається_з_файлу_ключа(self, tmp_path):
        key = tmp_path / "credentials.json"
        key.write_text(
            json.dumps({"type": "service_account", "client_email": "bot@project.iam.example"}),
            encoding="utf-8",
        )
        reader = SheetsReader("id", key)
        assert reader.service_account_email == "bot@project.iam.example"

    def test_биті_ключі_не_валять_бот(self, tmp_path):
        key = tmp_path / "credentials.json"
        key.write_text("це не json", encoding="utf-8")
        reader = SheetsReader("id", key)
        assert "не вдалося" in reader.service_account_email.lower()


class TestПомилкаДоступу:
    """403 від Google — найчастіша проблема при першому запуску.

    gspread перетворює її на голий PermissionError без жодного тексту.
    Якби ми його просто показали, людина побачила б слово «PermissionError»
    і не зрозуміла б, що робити. Тому пишемо власне пояснення з конкретною
    поштою, якою треба поділитися.
    """

    @pytest.fixture
    def reader(self, tmp_path):
        key = tmp_path / "credentials.json"
        key.write_text(
            json.dumps({"type": "service_account", "client_email": "bot@project.iam.example"}),
            encoding="utf-8",
        )
        return SheetsReader("spreadsheet-id", key)

    def test_permission_error_стає_зрозумілою_інструкцією(self, reader, monkeypatch):
        class FakeClient:
            def open_by_key(self, key):
                raise PermissionError

        monkeypatch.setattr(reader, "_connect", lambda: FakeClient())

        with pytest.raises(SheetsError) as info:
            reader._open_worksheet("Меджик")

        message = str(info.value)
        assert "403" in message
        assert "Поділитися" in message
        assert "bot@project.iam.example" in message, "має бути видно конкретну пошту"
        assert "Переглядач" in message

    def test_помилка_згадує_і_другу_можливу_причину(self, reader, monkeypatch):
        class FakeClient:
            def open_by_key(self, key):
                raise PermissionError

        monkeypatch.setattr(reader, "_connect", lambda: FakeClient())

        with pytest.raises(SheetsError, match="GOOGLE_SPREADSHEET_ID"):
            reader._open_worksheet("Меджик")

    def test_немає_файлу_ключа(self, tmp_path):
        reader = SheetsReader("id", tmp_path / "немає.json")
        with pytest.raises(SheetsError, match="GOOGLE_CREDENTIALS_FILE"):
            reader._connect()


class TestЗаглушка:
    def test_розділ_без_даних_не_ходить_у_мережу(self, columns_config, tmp_path):
        """«Сабміти» не мають навіть намагатися підключитися."""
        reader = SheetsReader("id", tmp_path / "немає.json")
        assert reader.read_section(columns_config.section("submits")) == []


class TestТількиЧитання:
    def test_scope_лише_на_читання(self):
        """Навіть за помилки в коді бот фізично не змінить таблицю."""
        from app.data.sheets import SCOPES

        assert SCOPES == ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        assert all("readonly" in scope for scope in SCOPES)
