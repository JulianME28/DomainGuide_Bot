"""Reproducible third offline audit of 400 compound user requests."""

from app.text.freeform import parse_free_text
from scripts.audit_300_queries import _countries, failures
from scripts.audit_400_queries_round3 import build_cases_round3


def test_third_400_compound_queries() -> None:
    failed: list[str] = []
    cases = build_cases_round3()
    for number, case in enumerate(cases, 1):
        parsed = parse_free_text(case.text)
        problems = failures(parsed, case.expected)
        if problems:
            query = parsed.query
            failed.append(
                f"#{number} [{case.category}] {case.text!r}: {problems}; "
                f"understood={parsed.understood}; countries={sorted(_countries(parsed))}; "
                f"excluded={sorted(item.code for item in query.excluded_countries)}; "
                f"dr=({query.dr_min},{query.dr_max}); traffic=({query.traffic_min},{query.traffic_max}); "
                f"spam=({query.spam_min},{query.spam_max}); both={parsed.both_bases}; "
                f"ambiguous={parsed.ambiguous_bases}; unrecognized={parsed.unrecognized}"
            )

    assert len(cases) == 400
    assert not failed, "\n" + "\n".join(failed)
