"""Тести карти колонок і whitelist.

Whitelist — це захисний бар'єр. Тести перевіряють, що він справді не пускає
колонки, яких у ньому немає.
"""

from __future__ import annotations

import pytest

from app.data.columns import ColumnsConfigError, load_columns_config


class TestБойовийКонфіг:
    """Перевіряємо справжній config/columns.toml проєкту."""

    def test_конфіг_читається(self, columns_config):
        assert columns_config.sections

    def test_є_три_розділи(self, columns_config):
        assert set(columns_config.sections) == {"magic", "mordy", "submits"}

    def test_меджик_налаштований(self, columns_config):
        magic = columns_config.section("magic")
        assert magic.title == "Меджик"
        assert magic.sheet == "Меджик"
        assert magic.reads_data
        assert magic.columns["domain"] == "Domain"
        assert magic.columns["language"] == "Мова"
        assert magic.columns["dr"] == "DR"
        assert magic.columns["traffic"] == "Traffic"

    def test_колонки_гео_немає(self, columns_config):
        """Ключова відмінність проєкту: колонки країни в даних не існує.

        Якщо цей тест колись впаде — значить, у карті колонок з'явилося гео,
        і модель «зона + мова» треба переглядати свідомо, а не випадково.
        """
        for section in columns_config.sections.values():
            assert "geo" not in section.columns
            assert "country" not in section.columns

    def test_морди_мають_аналіз_заспамленості(self, columns_config):
        """У «Морд» ті самі базові колонки плюс вихідні лінки й заспамленість."""
        mordy = columns_config.section("mordy")
        assert mordy.reads_data
        assert set(mordy.columns) == {"domain", "language", "dr", "traffic", "outlinks", "spam"}
        assert mordy.columns["outlinks"] == "Вихідні"
        assert mordy.columns["spam"] == "Заспамленість"

    def test_сабміти_це_заглушка(self, columns_config):
        submits = columns_config.section("submits")
        assert not submits.reads_data, "Сабміти не мають читати дані"
        assert submits.sheet == ""

    def test_морди_відстежують_заспамленість(self, columns_config):
        """Обидві колонки на місці — аналіз заспамленості ввімкнений."""
        assert columns_config.section("mordy").has_outlinks
        assert columns_config.section("mordy").tracks_spam

    def test_меджик_без_заспамленості(self, columns_config):
        """У «Меджика» цих колонок немає й не буде — аналіз вимкнений."""
        magic = columns_config.section("magic")
        assert "outlinks" not in magic.columns
        assert "spam" not in magic.columns
        assert not magic.has_outlinks
        assert not magic.tracks_spam

    def test_службова_колонка_raw_не_читається(self, columns_config):
        """raw — сире службове значення, у карті колонок його бути не має."""
        for section in columns_config.sections.values():
            assert "raw" not in section.columns.values()
        assert "raw" not in columns_config.whitelist

    def test_усі_колонки_в_whitelist(self, columns_config):
        for section in columns_config.sections.values():
            for header in section.columns.values():
                assert header in columns_config.whitelist

    def test_невідомий_розділ_дає_зрозумілу_помилку(self, columns_config):
        with pytest.raises(ColumnsConfigError, match="не описаний"):
            columns_config.section("немає_такого")


class TestПеревіркиКонфігу:
    """Тепер підсуваємо навмисно зіпсовані конфіги й перевіряємо реакцію."""

    def _write(self, tmp_path, text: str):
        path = tmp_path / "columns.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_колонка_поза_whitelist_блокується(self, tmp_path):
        """ГОЛОВНИЙ ТЕСТ БЕЗПЕКИ: колонку не з whitelist читати не можна."""
        path = self._write(
            tmp_path,
            """
[whitelist]
allowed = ["Domain", "Мова", "DR", "Traffic"]

[sections.magic]
title = "Меджик"
sheet = "Меджик"
enabled = true

[sections.magic.columns]
domain = "Domain"
language = "Мова"
dr = "DR"
traffic = "Traffic"
outlinks = "Ціна закупівлі"
""",
        )
        with pytest.raises(ColumnsConfigError, match="whitelist"):
            load_columns_config(path)

    def test_відсутня_обовязкова_колонка(self, tmp_path):
        path = self._write(
            tmp_path,
            """
[whitelist]
allowed = ["Domain", "Мова", "DR", "Traffic"]

[sections.magic]
sheet = "Меджик"
enabled = true

[sections.magic.columns]
domain = "Domain"
language = "Мова"
""",
        )
        with pytest.raises(ColumnsConfigError, match="dr"):
            load_columns_config(path)

    def test_друкарська_помилка_в_ролі(self, tmp_path):
        path = self._write(
            tmp_path,
            """
[whitelist]
allowed = ["Domain", "Мова", "DR", "Traffic"]

[sections.magic]
sheet = "Меджик"
enabled = true

[sections.magic.columns]
domain = "Domain"
language = "Мова"
dr = "DR"
trafic = "Traffic"
""",
        )
        with pytest.raises(ColumnsConfigError, match="невідому роль"):
            load_columns_config(path)

    def test_немає_whitelist(self, tmp_path):
        path = self._write(tmp_path, '[sections.magic]\nsheet = "Меджик"\n')
        with pytest.raises(ColumnsConfigError, match="whitelist"):
            load_columns_config(path)

    def test_файлу_немає(self, tmp_path):
        with pytest.raises(ColumnsConfigError, match="Не знайдено"):
            load_columns_config(tmp_path / "нема.toml")

    def test_розділ_без_аркуша_не_потребує_колонок(self, tmp_path):
        """«Сабміти» — заглушка, обов'язкові колонки з неї не вимагаються."""
        path = self._write(
            tmp_path,
            """
[whitelist]
allowed = ["Domain"]

[sections.submits]
title = "Сабміти"
sheet = ""
enabled = false
""",
        )
        config = load_columns_config(path)
        assert not config.section("submits").reads_data
