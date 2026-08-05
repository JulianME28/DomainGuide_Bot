"""Offline regression audit of 300 free-form bot requests (no API or secrets)."""

from __future__ import annotations

from dataclasses import dataclass

from app.text.freeform import ParsedQuery, parse_free_text


@dataclass(frozen=True)
class Case:
    category: str
    text: str
    expected: dict[str, object]


COUNTRIES = (
    ("Німеччина", "de"),
    ("Франція", "fr"),
    ("Канада", "ca"),
    ("Болгарія", "bg"),
    ("Британія", "gb"),
)
LANGUAGES = (("англійською", "en"), ("німецькою", "de"), ("французькою", "fr"))


def build_cases() -> list[Case]:
    cases: list[Case] = []

    def add(category: str, text: str, **expected: object) -> None:
        cases.append(Case(category, text, expected))

    for index in range(50):
        name, code = COUNTRIES[index % 5]
        value = 10 + index % 10 * 5
        if index % 2 == 0:
            add(
                "легкі",
                f"Меджик {name} DR від {value}",
                section="magic",
                countries={code},
                dr_min=value,
            )
        else:
            add(
                "легкі",
                f"Морди {name} трафік від {value}",
                section="mordy",
                countries={code},
                traffic_min=value,
            )

    for index in range(50):
        name, code = COUNTRIES[index % 5]
        dr, traffic, mode = 20 + index % 15, 100 + index % 10 * 25, index % 5
        if mode == 0:
            add(
                "середні",
                f"Меджик {name} DR від {dr} трафік від {traffic}",
                countries={code},
                dr_min=dr,
                traffic_min=traffic,
            )
        elif mode == 1:
            language, language_code = LANGUAGES[index % 3]
            add(
                "середні",
                f"Морди {name} {language} DR до {dr}",
                countries={code},
                languages={language_code},
                dr_max=dr,
            )
        elif mode == 2:
            add(
                "середні",
                f"Меджик {name}, гео Польща, трафік від {traffic}",
                countries={code},
                geo="pl",
                traffic_min=traffic,
            )
        elif mode == 3:
            add("середні", f"Морди у зоні .com DR від {dr}", zones={".com"}, dr_min=dr)
        else:
            add("середні", f"Морди {name} заспамленість до {dr}", countries={code}, spam_max=dr)

    forms = ("Меджик і Морди", "Морди та Меджик", "в обох базах", "по обох базах", "Меджик + Морди")
    for index in range(50):
        name, code = COUNTRIES[index % 5]
        value = 20 + index % 20
        add(
            "обидві бази",
            f"{forms[index % 5]} {name} DR від {value}",
            both_bases=True,
            countries={code},
            dr_min=value,
        )

    for index in range(50):
        name, code = COUNTRIES[index % 5]
        value = 5 + index % 25
        metric = "traffic_min" if index % 2 else "dr_min"
        label = "трафік" if index % 2 else "DR"
        add(
            "без бази",
            f"{name} {label} від {value}",
            section="magic",
            section_named=False,
            countries={code},
            **{metric: value},
        )

    for index in range(50):
        value, mode = 20 + index % 15, index % 5
        name, code = COUNTRIES[index % 5]
        if mode == 0:
            add(
                "складні",
                f"Меджик Німеччина Канада Франція DR від {value} трафік від 100",
                countries={"de", "ca", "fr"},
                dr_min=value,
                traffic_min=100,
            )
        elif mode == 1:
            add(
                "складні",
                f"Морди треба Німеччина {value} Канада {value + 2}, чи вистачає з трафіком 50+",
                countries={"de", "ca"},
                wants_coverage=True,
            )
        elif mode == 2:
            add(
                "складні",
                f"Меджик {name} DR будь-який трафік від 100",
                countries={code},
                traffic_min=100,
                dr_none=True,
            )
        elif mode == 3:
            add(
                "складні",
                f"Морди {name} DR від {value}, якщо мало — англомовні альтернативи",
                countries={code},
                dr_min=value,
                request_marker=True,
            )
        else:
            add(
                "складні",
                f"Меджик які країни мають DR від {value}",
                dr_min=value,
                wants_country_breakdown=True,
            )

    errors = (
        ("покажи найкращі домени", {"clarify": True}),
        ("Меджик Атлантида DR від 20", {"unrecognized": True}),
        ("Морди DR від 50 до 20", {"inverted_dr": True}),
        ("Німечина трафік від 100", {"countries": {"de"}}),
        ("Меджик трафік від сто", {"metric_missing": True}),
        ("не Німеччина DR від 20", {"negated_country_safe": True}),
        ("Морди крім зони .com DR від 30", {"zone_not_positive": True}),
        ("дай нормальні донори", {"clarify": True}),
        ("Меджик DR приблизно багато", {"metric_missing": True}),
        ("Франція або Канада DR від 20", {"countries": {"fr", "ca"}}),
    )
    for index in range(50):
        text, expected = errors[index % len(errors)]
        add("помилкові", f"{text} #{index + 1}", **expected)

    assert len(cases) == 300
    return cases


def _countries(parsed: ParsedQuery) -> set[str]:
    query = parsed.query
    return {item.code for item in query.countries} or (
        {query.country.code} if query.country else set()
    )


def failures(parsed: ParsedQuery, expected: dict[str, object]) -> list[str]:
    query = parsed.query
    actual_countries = _countries(parsed)
    languages = {item.code for item in query.languages} or (
        {query.language.code} if query.language else set()
    )
    excluded_countries = {item.code for item in query.excluded_countries}
    checks = {
        "section": lambda value: query.section_key == value,
        "section_named": lambda value: parsed.section_named == value,
        "countries": lambda value: actual_countries == value,
        "excluded_countries": lambda value: excluded_countries == value,
        "languages": lambda value: languages == value,
        "geo": lambda value: (query.geo.code if query.geo else None) == value,
        "zones": lambda value: set(query.zones) == value,
        "dr_min": lambda value: query.dr_min == float(value),
        "dr_max": lambda value: query.dr_max == float(value),
        "traffic_min": lambda value: query.traffic_min == float(value),
        "traffic_max": lambda value: query.traffic_max == float(value),
        "spam_min": lambda value: query.spam_min == float(value),
        "spam_max": lambda value: query.spam_max == float(value),
        "both_bases": lambda value: parsed.both_bases == value,
        "wants_coverage": lambda value: parsed.wants_coverage == value,
        "request_marker": lambda value: parsed.request_marker == value,
        "wants_country_breakdown": lambda value: parsed.wants_country_breakdown == value,
        "dr_none": lambda _value: query.dr_min is None and query.dr_max is None,
        "traffic_none": lambda _value: query.traffic_min is None and query.traffic_max is None,
        "spam_none": lambda _value: query.spam_min is None and query.spam_max is None,
        "clarify": lambda _value: not parsed.understood,
        "unrecognized": lambda _value: bool(parsed.unrecognized),
        "inverted_dr": lambda _value: (
            query.dr_min is not None and query.dr_max is not None and query.dr_min <= query.dr_max
        ),
        "metric_missing": lambda _value: all(
            getattr(query, field) is None
            for field in ("dr_min", "dr_max", "traffic_min", "traffic_max")
        ),
        "negated_country_safe": lambda _value: "de" not in actual_countries,
        "excluded_country_safe": lambda _value: "fr" not in actual_countries,
        "zone_not_positive": lambda _value: ".com" not in query.zones,
        "inverted_traffic": lambda _value: (
            query.traffic_min is not None
            and query.traffic_max is not None
            and query.traffic_min <= query.traffic_max
        ),
        "negated_metric_safe": lambda _value: query.dr_min is None and query.dr_max is None,
        "ambiguous_bases": lambda _value: parsed.ambiguous_bases,
    }
    return [key for key, value in expected.items() if not checks[key](value)]


def main() -> None:
    cases = build_cases()
    stats: dict[str, list[int]] = {}
    failed: list[tuple[int, Case, list[str], ParsedQuery]] = []
    for number, case in enumerate(cases, 1):
        parsed = parse_free_text(case.text)
        problems = failures(parsed, case.expected)
        stats.setdefault(case.category, [0, 0])[0] += 1
        if problems:
            stats[case.category][1] += 1
            failed.append((number, case, problems, parsed))

    print(f"TOTAL={len(cases)} PASS={len(cases) - len(failed)} FAIL={len(failed)}")
    for category, (total, bad) in stats.items():
        print(f"CATEGORY={category} TOTAL={total} PASS={total - bad} FAIL={bad}")
    for number, case, problems, parsed in failed:
        query = parsed.query
        print(
            f"FAIL #{number} [{case.category}] {case.text!r}; fields={problems}; "
            f"understood={parsed.understood}; countries={sorted(_countries(parsed))}; "
            f"unrecognized={parsed.unrecognized}; dr=({query.dr_min},{query.dr_max}); "
            f"traffic=({query.traffic_min},{query.traffic_max}); zones={query.zones}"
        )


if __name__ == "__main__":
    main()
