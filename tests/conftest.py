"""Спільні заготовки для всіх тестів (pytest називає їх «фікстурами»)."""

from __future__ import annotations

import pytest

from app.data.columns import load_columns_config
from app.data.repository import DonorRepository
from tests.fixtures.fake_data import FakeReader, empty_rows, magic_rows, mordy_rows


@pytest.fixture
def columns_config():
    """Справжня карта колонок із config/columns.toml.

    Беремо саме бойовий файл, а не вигаданий: так тести заодно перевіряють,
    що конфіг проєкту коректний.
    """
    return load_columns_config()


@pytest.fixture
def fake_reader():
    """Читач-підміна: «Меджик» із даними, «Морди» порожні."""
    return FakeReader({"magic": magic_rows(), "mordy": empty_rows()})


@pytest.fixture
def repository(fake_reader, columns_config):
    """Сховище донорів на фейкових даних."""
    return DonorRepository(fake_reader, columns_config, ttl_seconds=1800)


@pytest.fixture
async def magic(repository):
    """Готова база «Меджик» — найчастіше потрібна саме вона."""
    return await repository.get("magic")


@pytest.fixture
def spam_reader():
    """Читач-підміна, де «Морди» вже з аналізом заспамленості.

    Окремо від fake_reader навмисно: багато тестів покладаються на те, що в
    базовій фікстурі «Морди» порожні («0 донорів без падіння»), і ламати їх
    не можна. Тести заспамленості беруть саме цю фікстуру.
    """
    return FakeReader({"magic": magic_rows(), "mordy": mordy_rows()})


@pytest.fixture
def spam_repository(spam_reader, columns_config):
    return DonorRepository(spam_reader, columns_config, ttl_seconds=1800)


@pytest.fixture
async def mordy(spam_repository):
    """Готова база «Морди» з вихідними лінками й заспамленістю."""
    return await spam_repository.get("mordy")
