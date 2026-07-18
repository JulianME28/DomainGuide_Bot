"""Спільні заготовки для всіх тестів (pytest називає їх «фікстурами»)."""

from __future__ import annotations

import pytest

from app.data.columns import load_columns_config
from app.data.repository import DonorRepository
from tests.fixtures.fake_data import FakeReader, empty_rows, magic_rows


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
