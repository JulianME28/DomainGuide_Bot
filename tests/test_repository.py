"""Тести сховища донорів: читання, кеш, стійкість до помилок."""

from __future__ import annotations

from app.data.repository import DonorRepository, build_donors
from tests.fixtures.fake_data import (
    EXPECTED_DONORS,
    EXPECTED_SKIPPED,
    FakeReader,
    magic_rows,
)


class TestBuildDonors:
    def test_кількість_донорів(self):
        donors, skipped = build_donors(magic_rows())
        assert len(donors) == EXPECTED_DONORS
        assert skipped == EXPECTED_SKIPPED

    def test_повністю_порожній_рядок_не_вважається_помилкою(self):
        rows = [{"domain": "", "language": "", "dr": "", "traffic": ""}]
        donors, skipped = build_donors(rows)
        assert donors == ()
        assert skipped == 0, "порожній хвіст таблиці — це не зіпсований рядок"

    def test_рядок_з_даними_але_без_домену_рахується_пропущеним(self):
        rows = [{"domain": "", "language": "Polish", "dr": "10", "traffic": "10"}]
        donors, skipped = build_donors(rows)
        assert donors == ()
        assert skipped == 1

    def test_зони_визначено(self):
        donors, _ = build_donors(magic_rows())
        zones = {d.zone for d in donors}
        assert ".de" in zones
        assert ".co.uk" in zones
        assert ".com.tr" in zones

    def test_url_перетворюється_на_чистий_домен(self):
        """У фейкових даних один донор заданий повним посиланням."""
        donors, _ = build_donors(magic_rows())
        uk1 = next(d for d in donors if d.domain.startswith("uk1"))
        assert uk1.domain == "uk1.co.uk"
        assert uk1.zone == ".co.uk"

    def test_na_у_dr_стає_none_а_донор_лишається(self):
        donors, _ = build_donors(magic_rows())
        de2 = next(d for d in donors if d.domain == "de2.de")
        assert de2.dr is None, "n/a — це «значення немає»"
        assert de2.traffic == 1200.0, "а трафік 1,200 має розібратися"

    def test_мова_нормалізується(self):
        donors, _ = build_donors(magic_rows())
        languages = {d.language for d in donors}
        # У даних були "German", "german" і "German " — усе це одна мова.
        assert "german" in languages
        assert "German" not in languages
        assert "German " not in languages

    def test_не_падає_на_биті_дані(self):
        rows = [
            {"domain": "ok.de", "language": None, "dr": None, "traffic": None},
            {"domain": "!!!", "language": "???", "dr": "багато", "traffic": "трохи"},
            {},
        ]
        donors, _ = build_donors(rows)
        assert donors[0].dr is None
        assert all(d.traffic is None for d in donors)


class TestRepository:
    async def test_меджик_читається(self, repository):
        dataset = await repository.get("magic")
        assert dataset.available
        assert dataset.count == EXPECTED_DONORS
        assert dataset.rows_skipped == EXPECTED_SKIPPED

    async def test_порожній_аркуш_це_нуль_а_не_помилка(self, repository):
        """«Морди» зараз порожні — бот має спокійно сказати «0», а не впасти."""
        dataset = await repository.get("mordy")
        assert dataset.available
        assert dataset.count == 0
        assert dataset.is_empty

    async def test_сабміти_не_ходять_у_таблицю(self, repository, fake_reader):
        dataset = await repository.get("submits")
        assert not dataset.available
        assert dataset.count == 0
        assert "submits" not in fake_reader.calls, "заглушка не має читати дані"

    async def test_кеш_працює(self, repository, fake_reader):
        await repository.get("magic")
        await repository.get("magic")
        await repository.get("magic")
        assert fake_reader.calls.count("magic") == 1, "дані мають читатися один раз"

    async def test_refresh_перечитує(self, repository, fake_reader):
        await repository.get("magic")
        await repository.refresh("magic")
        assert fake_reader.calls.count("magic") == 2

    async def test_протухлий_кеш_перечитується(self, fake_reader, columns_config):
        repo = DonorRepository(fake_reader, columns_config, ttl_seconds=0)
        await repo.get("magic")
        await repo.get("magic")
        assert fake_reader.calls.count("magic") == 2

    async def test_помилка_читання_не_валить_бот(self, columns_config):
        reader = FakeReader(errors={"magic": "Google недоступний"})
        repo = DonorRepository(reader, columns_config)

        dataset = await repo.get("magic")

        assert not dataset.available
        assert dataset.count == 0
        assert "Google недоступний" in (dataset.error or "")

    async def test_після_помилки_бот_пробує_ще_раз(self, columns_config):
        """Невдале читання не має «залипати» в кеші на пів години."""
        reader = FakeReader(errors={"magic": "тимчасовий збій"})
        repo = DonorRepository(reader, columns_config)

        await repo.get("magic")
        await repo.get("magic")

        assert reader.calls.count("magic") == 2

    async def test_прогрів_не_падає_навіть_якщо_база_недоступна(self, columns_config):
        reader = FakeReader({"mordy": []}, errors={"magic": "збій"})
        repo = DonorRepository(reader, columns_config)

        await repo.warmup()  # не має кинути виняток

        assert repo.peek("magic") is not None
        assert not repo.peek("magic").available
        assert repo.peek("mordy").available

    async def test_прогрів_завантажує_робочі_бази(self, repository):
        await repository.warmup()
        assert repository.peek("magic").count == EXPECTED_DONORS
        assert repository.peek("mordy").count == 0

    async def test_вік_кешу_рахується(self, repository):
        assert repository.age_seconds("magic") is None
        await repository.get("magic")
        age = repository.age_seconds("magic")
        assert age is not None and age >= 0
