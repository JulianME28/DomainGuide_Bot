"""Карта колонок і whitelist — читається з config/columns.toml.

Навіщо це окремим файлом, а не в коді: якщо в таблиці перейменують колонку,
правити треба ТІЛЬКИ config/columns.toml, а код не чіпати взагалі.

Whitelist — це вимога безпеки. Бот прочитає з таблиці лише ті колонки, назви
яких є в списку `allowed`. Якщо в таблицю додадуть комерційно чутливий
стовпчик — бот його просто не побачить, поки назву не додадуть у whitelist.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from app.settings import PROJECT_ROOT

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "columns.toml"

# Ролі колонок, без яких база не працює.
REQUIRED_ROLES = ("domain", "language", "dr", "traffic")
# Ролі, які можуть з'явитися пізніше (аналіз заспамленості для «Морд»).
OPTIONAL_ROLES = ("outlinks",)
KNOWN_ROLES = frozenset(REQUIRED_ROLES + OPTIONAL_ROLES)


class ColumnsConfigError(RuntimeError):
    """Помилка в config/columns.toml. Текст бачить людина — має бути зрозумілий."""


@dataclass(frozen=True, slots=True)
class SectionConfig:
    """Налаштування одного розділу бота (= одного аркуша таблиці)."""

    key: str
    """Технічний ключ: "magic", "mordy", "submits"."""

    title: str
    """Назва для людини: «Меджик»."""

    sheet: str
    """Точна назва аркуша в Google Sheets. Порожня — аркуш не читається."""

    enabled: bool
    """Чи читаємо цей аркуш."""

    columns: Mapping[str, str]
    """Роль → назва колонки в таблиці. Наприклад: {"dr": "DR"}."""

    @property
    def reads_data(self) -> bool:
        """Чи цей розділ реально ходить у таблицю.

        «Сабміти» — заглушка: розділ у меню є, але даних не торкається.
        """
        return self.enabled and bool(self.sheet) and bool(self.columns)

    @property
    def has_outlinks(self) -> bool:
        """Чи підключена колонка вихідних лінків (аналіз заспамленості)."""
        return "outlinks" in self.columns


@dataclass(frozen=True, slots=True)
class ColumnsConfig:
    """Уся карта колонок разом із whitelist."""

    whitelist: frozenset[str]
    sections: Mapping[str, SectionConfig]

    def section(self, key: str) -> SectionConfig:
        try:
            return self.sections[key]
        except KeyError:
            raise ColumnsConfigError(
                f"Розділ «{key}» не описаний у config/columns.toml. "
                f"Доступні розділи: {', '.join(self.sections)}."
            ) from None

    @property
    def data_sections(self) -> tuple[SectionConfig, ...]:
        """Розділи, які реально читають дані (без заглушок)."""
        return tuple(s for s in self.sections.values() if s.reads_data)


def load_columns_config(path: str | Path | None = None) -> ColumnsConfig:
    """Читає config/columns.toml і перевіряє, що там усе гаразд."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise ColumnsConfigError(
            f"Не знайдено файл карти колонок: {config_path}\n"
            "Без нього бот не знає, у якому стовпчику таблиці що лежить."
        )

    try:
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ColumnsConfigError(
            f"Файл {config_path.name} пошкоджено — не вдалося прочитати.\nДеталі: {exc}"
        ) from exc

    whitelist = _read_whitelist(raw, config_path.name)
    sections = _read_sections(raw, whitelist, config_path.name)

    return ColumnsConfig(whitelist=whitelist, sections=MappingProxyType(sections))


def _read_whitelist(raw: dict, filename: str) -> frozenset[str]:
    """Дістає і перевіряє список дозволених колонок."""
    block = raw.get("whitelist")
    if not isinstance(block, dict) or "allowed" not in block:
        raise ColumnsConfigError(
            f"У файлі {filename} немає розділу [whitelist] зі списком allowed.\n"
            'Приклад:\n[whitelist]\nallowed = ["Domain", "Мова", "DR", "Traffic"]'
        )

    allowed = block["allowed"]
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ColumnsConfigError(f"У файлі {filename} whitelist.allowed має бути списком назв.")

    names = frozenset(item.strip() for item in allowed if item.strip())
    if not names:
        raise ColumnsConfigError(
            f"У файлі {filename} whitelist.allowed порожній — бот не зможе прочитати нічого."
        )
    return names


def _read_sections(raw: dict, whitelist: frozenset[str], filename: str) -> dict[str, SectionConfig]:
    """Дістає і перевіряє описи розділів."""
    blocks = raw.get("sections")
    if not isinstance(blocks, dict) or not blocks:
        raise ColumnsConfigError(f"У файлі {filename} немає жодного розділу [sections.*].")

    sections: dict[str, SectionConfig] = {}

    for key, block in blocks.items():
        if not isinstance(block, dict):
            raise ColumnsConfigError(f"Розділ [sections.{key}] у {filename} описано неправильно.")

        title = str(block.get("title", key)).strip() or key
        sheet = str(block.get("sheet", "")).strip()
        enabled = bool(block.get("enabled", True))
        columns = _read_columns(block.get("columns"), key, whitelist, filename)

        # Якщо розділ увімкнено і в нього є аркуш — обов'язкові колонки мають бути.
        if enabled and sheet:
            missing = [role for role in REQUIRED_ROLES if role not in columns]
            if missing:
                raise ColumnsConfigError(
                    f"У розділі [sections.{key}.columns] файлу {filename} не вказано: "
                    f"{', '.join(missing)}.\n"
                    f"Обов'язкові ролі колонок: {', '.join(REQUIRED_ROLES)}."
                )

        sections[key] = SectionConfig(
            key=key,
            title=title,
            sheet=sheet,
            enabled=enabled,
            columns=MappingProxyType(columns),
        )

    return sections


def _read_columns(
    block: object, section_key: str, whitelist: frozenset[str], filename: str
) -> dict[str, str]:
    """Перевіряє блок [sections.X.columns]: ролі відомі, колонки — у whitelist."""
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ColumnsConfigError(
            f"Блок [sections.{section_key}.columns] у {filename} має бути переліком "
            "«роль = назва колонки»."
        )

    columns: dict[str, str] = {}
    for role, header in block.items():
        if role not in KNOWN_ROLES:
            raise ColumnsConfigError(
                f"У [sections.{section_key}.columns] вказано невідому роль «{role}».\n"
                f"Можливі ролі: {', '.join(sorted(KNOWN_ROLES))}. Схоже на друкарську помилку."
            )
        if not isinstance(header, str) or not header.strip():
            raise ColumnsConfigError(
                f"Роль «{role}» у розділі {section_key} має вказувати назву колонки текстом."
            )

        header = header.strip()
        # ГОЛОВНА ПЕРЕВІРКА БЕЗПЕКИ: колонки поза whitelist читати не можна.
        if header not in whitelist:
            raise ColumnsConfigError(
                f"Колонка «{header}» (роль {role}, розділ {section_key}) відсутня у whitelist.\n"
                "Бот читає лише дозволені колонки. Якщо ця колонка справді потрібна — "
                "додайте її назву в [whitelist] allowed у тому ж файлі."
            )
        columns[role] = header

    return columns
