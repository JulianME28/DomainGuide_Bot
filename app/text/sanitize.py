"""Санітарна сітка для готового DonorQuery — ОДНА на обидва шляхи (словник і ШІ).

Навіщо. Словниковий розбір гасить неможливе під час парсингу: перевернутий
діапазон (`_sane_range` у dimensions.py) і заперечену зону (`_extract_zone` у
freeform.py). Фільтр від ШІ (interpret_json) раніше цю обробку ОБМИНАВ — і давав
тихі хибні числа: «DR від 40 до 20» → нуль; заперечена зона → позитивний фільтр.

Тут та сама логіка, переВикористана (а не продубльована), застосовується до
готового DonorQuery. ШІ-шлях кличе sanitize_query одразу після interpret_json;
словниковий шлях уже сан-обробляється під час парсингу тими самими функціями.

БЕЗПЕКА: сітка лише ГАСИТЬ неможливе (звужує/прибирає фільтр), доступу до даних
не додає, whitelist полів не чіпає. ШІ лишається перекладачем.
"""

from __future__ import annotations

from app.analytics.query import DonorQuery
from app.text.dimensions import _sane_range
from app.text.freeform import find_negated_zones


def sanitize_query(query: DonorQuery | None, text: str) -> DonorQuery | None:
    """Проводить фільтр через ту саму санітарну обробку, що й словниковий шлях.

    1. Перевернуті діапазони (min > max) для DR / трафіку / заспамленості →
       лишаємо нижній поріг, скидаємо верхній (`_sane_range`, група E). Це прибирає
       тихий нуль на кшталт «DR від 40 до 20».
    2. Заперечені в тексті зони («не .com») знімаємо з query.zones, щоб не було
       хибного ПОЗИТИВНОГО фільтра. Знімаємо лише ЗОНИ — країни не чіпаємо (у
       «морди Франція але не .fr» країна Франція — це легітимна позитивна умова).

    query=None (фільтра немає) повертаємо як є.
    """
    if query is None:
        return None

    dr_min, dr_max = _sane_range(query.dr_min, query.dr_max)
    traffic_min, traffic_max = _sane_range(query.traffic_min, query.traffic_max)
    spam_min, spam_max = _sane_range(query.spam_min, query.spam_max)

    zones = query.zones
    negated = set(find_negated_zones(text))
    if negated and zones:
        zones = tuple(zone for zone in zones if zone not in negated)

    return query.replace(
        dr_min=dr_min,
        dr_max=dr_max,
        traffic_min=traffic_min,
        traffic_max=traffic_max,
        spam_min=spam_min,
        spam_max=spam_max,
        zones=zones,
    )
