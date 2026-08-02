"""Операція ПОКРИТТЯ: скільки донорів треба по країнах — і чи вистачає їх.

Крок 2 (гнучкі числові операції). ШІ лише РОЗКЛАДАЄ запит у CoverageQuery
(яку країну скільки треба, які пороги трафіку перевірити); рахує все цей
рушій, детерміновано. Домени/донори назовні не йдуть — лише кількості.

Як рахуємо. Для кожного порогу трафіку — ОДИН прохід по базі (реюз
recommendations._country_totals): він дає, скільки донорів у КОЖНІЙ потрібній
країні за тією самою трикроковою моделлю країни, що й звичайна картка (зона →
GEO → мова). Звірено 1:1 з run_query на реальній базі.

Вердикт виносить КОД (не ШІ), від НАЙВИЩОГО названого порогу:
  * count(найвищий) ≥ треба            → ✅ вистачає;
  * count(0) ≥ треба > count(найвищий) → ⚠️ всього досить, якісних бракує;
  * count(0) < треба                   → ❌ навіть усього замало.
Дефіцит рахується від того ж порогу, що дав вердикт — одне джерело правди.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.analytics.query import CoverageQuery, DonorQuery
from app.analytics.recommendations import _country_totals
from app.data.models import Dataset
from app.dictionary.countries import Country


class CoverageVerdict(Enum):
    """Підсумок по одній країні. Значення — лише для стабільного порівняння."""

    ENOUGH = "enough"  # ✅ вистачає навіть на найвищому порозі
    LOW_QUALITY = "low"  # ⚠️ всього досить, але на найвищому порозі — ні
    SHORT = "short"  # ❌ навіть усього замало


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """Покриття по одній країні: потреба, кількості за порогами, вердикт, дефіцит."""

    country: Country
    need: int
    counts: tuple[tuple[int, int], ...]
    """(поріг, скільки донорів) — у тому ж порядку порогів, що й у запиті (з 0)."""

    verdict: CoverageVerdict
    deficit: int
    """Скількох бракує до потреби (0, якщо вистачає). Рахується від того ж порогу,
    що дав вердикт: ⚠️ — від найвищого, ❌ — від «всього» (поріг 0)."""

    @property
    def total(self) -> int:
        """Скільки всього (на порозі 0)."""
        return self.counts[0][1]

    @property
    def top_count(self) -> int:
        """Скільки на найвищому порозі — саме за ним вердикт."""
        return self.counts[-1][1]


@dataclass(frozen=True, slots=True)
class CoverageResult:
    """Готове покриття по всіх країнах запиту. Підсумки — властивостями звідси,
    щоб рядок «всього закрито / бракує» рахувався з ТИХ САМИХ рядків, що й показ."""

    section_key: str
    section_title: str
    thresholds: tuple[int, ...]
    rows: tuple[CoverageRow, ...]
    available: bool = True
    stale: bool = False
    error: str = ""

    @property
    def max_threshold(self) -> int:
        return self.thresholds[-1] if self.thresholds else 0

    @property
    def covered(self) -> tuple[CoverageRow, ...]:
        """Країни, де потреба закрита навіть на найвищому порозі (✅)."""
        return tuple(row for row in self.rows if row.verdict is CoverageVerdict.ENOUGH)

    @property
    def short(self) -> tuple[CoverageRow, ...]:
        """Країни, де чогось бракує (⚠️ або ❌), у порядку запиту."""
        return tuple(row for row in self.rows if row.verdict is not CoverageVerdict.ENOUGH)


def _verdict(need: int, total: int, top_count: int) -> tuple[CoverageVerdict, int]:
    """Вердикт + дефіцит по одній країні. Від найвищого порогу (див. модуль)."""
    if total < need:
        return CoverageVerdict.SHORT, need - total
    if top_count < need:
        return CoverageVerdict.LOW_QUALITY, need - top_count
    return CoverageVerdict.ENOUGH, 0


def run_coverage(dataset: Dataset, cq: CoverageQuery) -> CoverageResult:
    """Рахує покриття за потребою. Числа реальні (рушій), нічого не вигадується."""
    if not dataset.available:
        return CoverageResult(
            section_key=cq.section_key,
            section_title=dataset.title,
            thresholds=cq.thresholds,
            rows=(),
            available=False,
            error=dataset.error or "",
        )

    countries = tuple(country for country, _need in cq.needs)
    # Один прохід по базі на КОЖЕН поріг → скільки в кожній країні (див. модуль).
    totals_by_threshold: dict[int, dict[str, int]] = {}
    for threshold in cq.thresholds:
        base = DonorQuery(
            section_key=cq.section_key,
            traffic_min=float(threshold) if threshold > 0 else None,
        )
        totals_by_threshold[threshold] = _country_totals(dataset, base, countries)

    rows: list[CoverageRow] = []
    for country, need in cq.needs:
        counts = tuple(
            (threshold, totals_by_threshold[threshold][country.code]) for threshold in cq.thresholds
        )
        total = counts[0][1]
        top_count = counts[-1][1]
        verdict, deficit = _verdict(need, total, top_count)
        rows.append(
            CoverageRow(
                country=country,
                need=need,
                counts=counts,
                verdict=verdict,
                deficit=deficit,
            )
        )

    return CoverageResult(
        section_key=cq.section_key,
        section_title=dataset.title,
        thresholds=cq.thresholds,
        rows=tuple(rows),
        stale=dataset.stale,
    )


__all__ = [
    "CoverageResult",
    "CoverageRow",
    "CoverageVerdict",
    "run_coverage",
]
