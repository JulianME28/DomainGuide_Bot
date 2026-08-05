"""Сховище донорів у пам'яті: кеш, прогрів на старті, оновлення за кнопкою.

Навіщо кеш. В аркуші «Меджик» близько 29 000 рядків. Ходити за ними в Google
на кожен запит — це кілька секунд очікування щоразу. Тому дані читаються один
раз, лежать у пам'яті, і всі запити рахуються по пам'яті — це мілісекунди.

Як оновлюються дані:
  * при старті бота (прогрів);
  * коли минув TTL (за замовчуванням 30 хвилин);
  * коли адмін натиснув кнопку «Оновити дані».

Мережа блокує потік, тому читання загорнуте в asyncio.to_thread — поки бот
чекає на Google, він спокійно відповідає іншим користувачам.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from app.data.columns import ColumnsConfig, SectionConfig
from app.data.models import Dataset, Donor, StopList
from app.data.parsing import (
    extract_zone,
    normalize_domain,
    normalize_language,
    parse_geo,
    parse_number,
)
from app.logging_setup import get_logger

logger = get_logger(__name__)


class SectionReader(Protocol):
    """Те, що вміє читати розділ. Реальний — SheetsReader, у тестах — фейковий."""

    def read_section(self, section: SectionConfig) -> list[dict[str, str]]: ...

    def read_domain_list(self, sheet_name: str, header: str = "Domain") -> list[str]: ...


STOP_MORDY_SHEET = "Стоп Морди"


def build_donors(rows: list[dict[str, str]]) -> tuple[tuple[Donor, ...], int]:
    """Перетворює сирі рядки таблиці на список донорів.

    Повертає (донори, скільки рядків не вдалося розібрати).

    Правила:
      * рядок без домену — не донор (домен це «паспорт» донора);
      * повністю порожній рядок — це просто хвіст таблиці, не помилка;
      * биті DR/трафік стають None: донор рахується, але в середні не входить.
    """
    donors: list[Donor] = []
    skipped = 0

    for row in rows:
        domain = normalize_domain(row.get("domain", ""))
        if not domain:
            # Чи було в рядку хоч щось? Якщо ні — це порожній хвіст таблиці.
            if any(str(value).strip() for value in row.values()):
                skipped += 1
            continue

        geo_code, geo_traffic = parse_geo(row.get("geo"))
        donors.append(
            Donor(
                domain=domain,
                zone=extract_zone(domain),
                language=normalize_language(row.get("language", "")),
                dr=parse_number(row.get("dr")),
                traffic=parse_number(row.get("traffic")),
                # Ці ключі є лише в «Мордах». Для «Меджика» їх немає →
                # parse_number(None) → None, і донор просто без цих даних.
                outlinks=parse_number(row.get("outlinks")),
                spammed=parse_number(row.get("spam")),
                # GEO — навпаки, лише в «Меджику». Формат «(cc, N)».
                geo_code=geo_code,
                geo_traffic=geo_traffic,
            )
        )

    return tuple(donors), skipped


class DonorRepository:
    """Тримає бази в пам'яті й видає їх на запит."""

    def __init__(
        self,
        reader: SectionReader,
        config: ColumnsConfig,
        ttl_seconds: int = 1800,
    ) -> None:
        self._reader = reader
        self._config = config
        self._ttl = ttl_seconds
        self._cache: dict[str, Dataset] = {}
        self._stop_cache: StopList | None = None
        self._stop_lock = asyncio.Lock()
        # Окремий замок на кожен розділ: якщо двоє одночасно попросять «Меджик»,
        # у Google піде один запит, а не два.
        self._locks: dict[str, asyncio.Lock] = {}

    # -- публічне API --------------------------------------------------------

    async def get(self, section_key: str) -> Dataset:
        """Повертає базу. Якщо в кеші свіжа — миттєво, інакше читає з Google."""
        cached = self._cache.get(section_key)
        if cached is not None and self._is_fresh(cached):
            return cached
        return await self._load(section_key)

    async def get_stop_domains(self, *, force: bool = False) -> StopList:
        """Повертає стоп-лист Мордів; звичайні запити його не застосовують."""
        cached = self._stop_cache
        if (
            not force
            and cached is not None
            and cached.available
            and (time.time() - cached.loaded_at) < self._ttl
        ):
            return cached

        async with self._stop_lock:
            cached = self._stop_cache
            if (
                not force
                and cached is not None
                and cached.available
                and (time.time() - cached.loaded_at) < self._ttl
            ):
                return cached
            result = await asyncio.to_thread(self._read_stop_blocking)
            if not result.available and cached is not None and cached.available:
                return replace(cached, stale=True)
            self._stop_cache = result
            return result

    async def refresh(self, section_key: str | None = None) -> list[Dataset]:
        """Примусово перечитує дані (кнопка адміна «Оновити дані»).

        force=True принципово важливий: без нього свіжий кеш переміг би, і
        кнопка «Оновити дані» мовчки нічого б не робила.
        """
        keys = [section_key] if section_key else [s.key for s in self._config.data_sections]
        return [await self._load(key, force=True) for key in keys]

    async def warmup(self) -> None:
        """Прогрів на старті: читає всі робочі бази паралельно.

        Помилки тут не зупиняють бот — він запуститься й чесно скаже, що база
        недоступна. Краще працюючий бот з однією проблемною базою, ніж бот,
        який узагалі не піднявся.
        """
        sections = self._config.data_sections
        if not sections:
            logger.warning("Немає жодного розділу з даними — прогрівати нічого.")
            return

        logger.info("Прогрів кешу: %s", ", ".join(s.title for s in sections))
        started = time.perf_counter()

        results = await asyncio.gather(
            *(self._load(section.key) for section in sections),
            return_exceptions=True,
        )

        for section, result in zip(sections, results, strict=False):
            if isinstance(result, BaseException):
                logger.error("Не вдалося прогріти «%s»: %s", section.title, result)
            elif result.available:
                logger.info("«%s»: %d донорів.", section.title, result.count)
            else:
                logger.warning("«%s» недоступна: %s", section.title, result.error)

        logger.info("Прогрів завершено за %.1f с.", time.perf_counter() - started)

    def peek(self, section_key: str) -> Dataset | None:
        """Що зараз у кеші, без походу в мережу. Для адмін-статусу."""
        return self._cache.get(section_key)

    def snapshot(self) -> Mapping[str, Dataset]:
        """Знімок усього кешу — для адмінки."""
        return dict(self._cache)

    def age_seconds(self, section_key: str) -> float | None:
        """Скільки секунд тому оновлювалися дані. None — ще не читалися."""
        cached = self._cache.get(section_key)
        return None if cached is None else time.time() - cached.loaded_at

    # -- внутрішня кухня -----------------------------------------------------

    def _is_fresh(self, dataset: Dataset) -> bool:
        """Чи дані ще не застаріли.

        Невдале читання не кешуємо надовго: якщо база була недоступна,
        наступний запит спробує ще раз.
        """
        if not dataset.available:
            return False
        return (time.time() - dataset.loaded_at) < self._ttl

    def _lock_for(self, section_key: str) -> asyncio.Lock:
        lock = self._locks.get(section_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[section_key] = lock
        return lock

    async def _load(self, section_key: str, *, force: bool = False) -> Dataset:
        """Читає розділ із джерела й кладе в кеш.

        force=True — примусово, навіть якщо в кеші свіжі дані.
        """
        section = self._config.section(section_key)

        async with self._lock_for(section_key):
            # Поки чекали на замок, хтось міг уже завантажити — перевіряємо ще раз.
            # Але при примусовому оновленні кеш ігноруємо взагалі.
            cached = self._cache.get(section_key)
            if not force and cached is not None and self._is_fresh(cached):
                return cached

            dataset = await asyncio.to_thread(self._read_blocking, section)

            # Оновлення не вдалося, але в пам'яті Є попередні успішні дані.
            # Краще трохи старі числа, ніж жодних: віддаємо кеш із поміткою,
            # а сам кеш НЕ псуємо — його loaded_at лишається старим, тож
            # наступний запит спробує оновитися ще раз (і підхопить дані,
            # щойно мережа відновиться).
            if not dataset.available:
                previous = self._cache.get(section_key)
                if previous is not None and previous.available:
                    logger.warning(
                        "«%s»: оновлення не вдалося (%s) — віддаю дані з кешу станом на %s",
                        section.title,
                        dataset.error,
                        time.strftime("%H:%M:%S", time.localtime(previous.loaded_at)),
                    )
                    return replace(previous, stale=True)

            self._cache[section_key] = dataset
            return dataset

    def _read_blocking(self, section: SectionConfig) -> Dataset:
        """Синхронна частина: мережа + розбір рядків. Виконується в окремому потоці.

        Тут ловиться будь-яка помилка. Назовні завжди виходить Dataset —
        або з даними, або з поясненням, чому їх немає. Бот не падає ніколи.
        """
        now = time.time()

        # Розділ-заглушка («Сабміти») — просто порожня база без походу в мережу.
        if not section.reads_data:
            return Dataset(
                section_key=section.key,
                title=section.title,
                sheet_name=section.sheet,
                donors=(),
                loaded_at=now,
                available=False,
                error="Розділ поки не підключений до даних.",
            )

        try:
            rows = self._reader.read_section(section)
        except Exception as exc:
            logger.error("Помилка читання «%s»: %s", section.title, exc)
            return Dataset(
                section_key=section.key,
                title=section.title,
                sheet_name=section.sheet,
                donors=(),
                loaded_at=now,
                available=False,
                error=str(exc),
            )

        donors, skipped = build_donors(rows)

        if skipped:
            logger.warning("«%s»: %d рядків пропущено (немає домену).", section.title, skipped)

        return Dataset(
            section_key=section.key,
            title=section.title,
            sheet_name=section.sheet,
            donors=donors,
            loaded_at=now,
            available=True,
            rows_read=len(rows),
            rows_skipped=skipped,
            tracks_spam=section.tracks_spam,
            tracks_geo=section.has_geo,
        )

    def _read_stop_blocking(self) -> StopList:
        now = time.time()
        try:
            values = self._reader.read_domain_list(STOP_MORDY_SHEET)
            domains = frozenset(domain for value in values if (domain := normalize_domain(value)))
            logger.info("«%s»: завантажено %d доменів.", STOP_MORDY_SHEET, len(domains))
            return StopList(domains=domains, loaded_at=now)
        except Exception as exc:
            logger.error("Помилка читання «%s»: %s", STOP_MORDY_SHEET, exc)
            return StopList(domains=frozenset(), loaded_at=now, available=False, error=str(exc))
