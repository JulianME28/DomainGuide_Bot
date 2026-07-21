"""Розбір «брудних» значень із таблиці.

Дані в таблиці заповнювали люди, тому там буває що завгодно:
    DR:      "28", "n/a", "", "-", "28.5"
    Трафік:  "4 800", "1,200", "1.200", "12K", "<10"
    Мова:    "English", "english ", "  Turkish"

Завдання цього модуля — витягнути з такого сміття нормальне значення й
НІКОЛИ не впасти. Якщо значення розібрати неможливо — повертається None,
і бот спокійно працює далі.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Позначки «значення немає». Усе це перетворюється на None.
# ---------------------------------------------------------------------------
_MISSING_MARKERS = frozenset(
    {
        "",
        "-",
        "--",
        "—",  # довге тире —
        "–",  # середнє тире –
        "n/a",
        "n\\a",
        "na",
        "n.a.",
        "n.a",
        "#n/a",
        "null",
        "none",
        "nan",
        "нема",
        "немає",
        "нет",
        "н/д",
        "#н/д",
        "?",
        "??",
        "х",
        "x",
    }
)

# Усі види пробілів плюс апострофи — Google Sheets розділяє ними тисячі
# ("4 800", "4'800"). У Python \s ловить і звичайний пробіл, і нерозривний
# ( ), і вузький ( ), тому перелічувати їх окремо не треба.
_SEPARATOR_NOISE = re.compile(r"[\s'’` ]+")

# Множники в кінці числа: "12K" = 12 000.
_SUFFIX_MULTIPLIERS = {"k": 1_000, "к": 1_000, "m": 1_000_000, "м": 1_000_000, "b": 1_000_000_000}

# "1,234" або "12,345,678" — кома як роздільник тисяч.
_THOUSANDS_COMMA = re.compile(r"^\d{1,3}(,\d{3})+$")
# "1.234" або "12.345.678" — крапка як роздільник тисяч.
_THOUSANDS_DOT = re.compile(r"^\d{1,3}(\.\d{3})+$")

# Сміття в кінці числа: "1200 відвідувань" → "1200".
_TRAILING_JUNK = re.compile(r"[^\d.,kкmмb]+$")


# ---------------------------------------------------------------------------
# Складені доменні зони.
#
# Щоб зрозуміти зону домену, зазвичай досить останнього шматка після крапки:
# "example.de" → ".de". Але є зони з двох шматків: "bbc.co.uk" → ".co.uk".
#
# Список нижче — саме такі складені зони. Він навмисно ЯВНИЙ, а не «розумний»:
# інакше "mail.web.de" помилково перетворилося б на ".web.de" замість ".de".
# ---------------------------------------------------------------------------
MULTI_PART_SUFFIXES = frozenset(
    {
        # Британія
        "co.uk", "org.uk", "me.uk", "net.uk", "ltd.uk", "plc.uk", "ac.uk", "gov.uk", "sch.uk",
        # Австралія
        "com.au", "net.au", "org.au", "edu.au", "id.au",
        # Бразилія
        "com.br", "net.br", "org.br",
        # Індія
        "co.in", "net.in", "org.in", "firm.in", "gen.in", "ind.in",
        # ПАР
        "co.za", "org.za", "net.za", "web.za",
        # Туреччина
        "com.tr", "net.tr", "org.tr", "gen.tr", "web.tr",
        # Мексика
        "com.mx", "net.mx", "org.mx",
        # Аргентина
        "com.ar", "net.ar", "org.ar",
        # Ізраїль
        "co.il", "org.il", "net.il", "ac.il",
        # Індонезія
        "co.id", "or.id", "web.id", "my.id", "biz.id",
        # Колумбія (увага: гола ".co" — глобальна зона, а ".com.co" — Колумбія)
        "com.co", "net.co", "org.co",
        # Нова Зеландія
        "co.nz", "net.nz", "org.nz",
        # Корея
        "co.kr", "or.kr", "ne.kr",
        # Малайзія
        "com.my", "net.my", "org.my",
        # Китай / Гонконг / Тайвань
        "com.cn", "net.cn", "org.cn", "com.hk", "com.tw",
        # Близький Схід
        "com.sa", "net.sa", "org.sa", "com.eg", "net.eg", "org.eg",
        # Азія
        "com.ph", "net.ph", "com.bd", "net.bd", "com.pk", "net.pk", "org.pk",
        "com.vn", "net.vn", "org.vn", "co.th", "in.th", "ac.th", "com.sg",
        # Африка
        "com.ng", "net.ng", "org.ng",
        # Латинська Америка
        "com.pe", "net.pe", "com.ec", "com.uy", "com.ve", "com.do",
        "com.gt", "com.py", "com.bo", "com.cl",
        # Європа
        "com.ua", "net.ua", "org.ua", "in.ua", "kiev.ua",
        "com.pl", "net.pl", "org.pl", "com.pt", "net.pt", "org.pt",
        "com.es", "nom.es", "org.es", "com.gr", "net.gr", "org.gr",
        "com.hr", "com.ro", "com.cy", "com.mt",
    }
)  # fmt: skip


def parse_number(raw: object) -> float | None:
    """Витягує число з чого завгодно. Не падає ніколи.

    Приклади:
        "28"      → 28.0
        "n/a"     → None
        "4 800"   → 4800.0      (пробіл — роздільник тисяч)
        "1,200"   → 1200.0      (кома — роздільник тисяч)
        "1.200"   → 1200.0      (крапка — роздільник тисяч)
        "28.5"    → 28.5        (крапка — десяткова)
        "28,5"    → 28.5        (кома — десяткова)
        "12K"     → 12000.0
        "<10"     → 10.0
        "-5"      → None        (від'ємні DR/трафік — це сміття)
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw) if raw >= 0 else None

    text = str(raw).strip().lower()
    if text in _MISSING_MARKERS:
        return None

    # Прибираємо пробіли й апострофи (їх використовують як роздільник тисяч).
    text = _SEPARATOR_NOISE.sub("", text)
    if text in _MISSING_MARKERS:
        return None

    # Прибираємо приблизні позначки на початку: "<10", "~500", "+100".
    text = text.lstrip("<>~≈≥≤+")
    # Прибираємо хвости: "1200відвідувань" → "1200".
    text = _TRAILING_JUNK.sub("", text)
    if not text:
        return None

    # Множник у кінці: "12k" → 12 * 1000.
    multiplier = 1
    if text[-1] in _SUFFIX_MULTIPLIERS:
        multiplier = _SUFFIX_MULTIPLIERS[text[-1]]
        text = text[:-1]
        if not text:
            return None

    try:
        value = float(_normalize_separators(text))
    except ValueError:
        return None

    value *= multiplier
    # Від'ємний DR або трафік не має сенсу — вважаємо це помилкою в даних.
    return value if value >= 0 else None


def _normalize_separators(text: str) -> str:
    """Розбирається, де кома/крапка — це тисячі, а де — десяткова частина."""
    has_comma = "," in text
    has_dot = "." in text

    if has_comma and has_dot:
        # Обидва знаки разом: той, що правіше, — десятковий.
        # "1,234.56" → 1234.56    "1.234,56" → 1234.56
        if text.rfind(",") > text.rfind("."):
            return text.replace(".", "").replace(",", ".")
        return text.replace(",", "")

    if has_comma:
        # "1,200" виглядає як тисячі; "28,5" — як десяткова кома.
        return text.replace(",", "") if _THOUSANDS_COMMA.match(text) else text.replace(",", ".")

    if has_dot:
        # "1.200" виглядає як тисячі; "28.5" — як звичайне дробове число.
        return text.replace(".", "") if _THOUSANDS_DOT.match(text) else text

    return text


def normalize_language(raw: object) -> str:
    """Приводить назву мови до єдиного вигляду.

    "English" → "english",  " Turkish " → "turkish",  "" → "".
    Потрібно, бо в таблиці трапляються хвостові пробіли й різний регістр.
    """
    if raw is None:
        return ""
    # Спершу зводимо будь-які пробіли до звичайного, потім прибираємо краї.
    text = re.sub(r"\s+", " ", str(raw)).strip().lower()
    return "" if text in _MISSING_MARKERS else text


def normalize_domain(raw: object) -> str:
    """Чистить домен: прибирає http://, www., шлях, порт, зайві пробіли.

    "https://WWW.Example.co.uk/blog?x=1" → "example.co.uk"
    """
    if raw is None:
        return ""
    text = _SEPARATOR_NOISE.sub("", str(raw)).strip().lower()
    if text in _MISSING_MARKERS:
        return ""

    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)  # прибрати http:// чи https://
    text = text.split("/", 1)[0]  # прибрати шлях
    text = text.split("?", 1)[0].split("#", 1)[0]  # прибрати параметри
    text = text.split("@")[-1]  # якщо раптом пошта — лишити домен
    text = text.split(":", 1)[0]  # прибрати порт
    text = text.strip(".")

    if text.startswith("www."):
        text = text[4:]
    return text


# Формат колонки GEO у «Меджику»: рівно `(cc, N)` — двобуквений код країни
# й число трафіку. Наприклад: "(fr, 0)", "(us, 16640)".
_GEO_RE = re.compile(r"^\(\s*([a-zA-Z]{2})\s*,\s*(\d+)\s*\)$")


def parse_geo(raw: object) -> tuple[str, float | None]:
    """Розбирає значення GEO `(cc, N)`. Не падає ніколи.

    GEO — це країна ПОХОДЖЕННЯ ТРАФІКУ на донора плюс обсяг цього трафіку:
        "(fr, 5000)" → ("fr", 5000.0)
        "(fr, 0)"    → ("fr", 0.0)      ← країна відома, трафіку не виміряно
        ""           → ("", None)       ← GEO немає (60% рядків)
        "щось інше"  → ("", None)       ← невідповідний формат — теж «немає»

    Код повертається малими літерами. N=0 не відкидається тут (це валідне
    «нуль трафіку») — рішення «не рахувати такого» приймає вже модель країни.
    """
    if raw is None:
        return "", None
    match = _GEO_RE.match(str(raw).strip())
    if match is None:
        return "", None
    return match.group(1).lower(), parse_number(match.group(2))


def extract_zone(raw: object) -> str:
    """Визначає доменну зону — це головний сигнал країни в цьому проєкті.

    "example.de"        → ".de"
    "bbc.co.uk"         → ".co.uk"
    "shop.example.com"  → ".com"
    "щось-без-крапки"   → ""      (крапки немає — зони немає)
    """
    domain = normalize_domain(raw)
    labels = [part for part in domain.split(".") if part]
    if len(labels) < 2:
        return ""

    # Спершу перевіряємо складену зону з двох останніх шматків ("co.uk").
    if len(labels) >= 3:
        two_part = f"{labels[-2]}.{labels[-1]}"
        if two_part in MULTI_PART_SUFFIXES:
            return f".{two_part}"

    last = labels[-1]
    # Зона з цифр або з одного символу — це не зона (наприклад, IP-адреса).
    if len(last) < 2 or not last.isalpha():
        return ""
    return f".{last}"
