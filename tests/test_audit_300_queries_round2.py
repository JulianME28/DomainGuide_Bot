"""Другий відтворюваний offline-аудит 300 нових формулювань."""

from app.text.freeform import parse_free_text
from scripts.audit_300_queries import _countries, failures
from scripts.audit_300_queries_round2 import build_cases_round2


def test_другі_300_запитів() -> None:
    failed: list[str] = []
    cases = build_cases_round2()
    for number, case in enumerate(cases, 1):
        parsed = parse_free_text(case.text)
        problems = failures(parsed, case.expected)
        if problems:
            query = parsed.query
            failed.append(
                f"#{number} [{case.category}] {case.text!r}: {problems}; "
                f"understood={parsed.understood}; countries={sorted(_countries(parsed))}; "
                f"unrecognized={parsed.unrecognized}; dr=({query.dr_min},{query.dr_max}); "
                f"traffic=({query.traffic_min},{query.traffic_max}); both={parsed.both_bases}"
            )

    assert len(cases) == 300
    assert not failed, "\n" + "\n".join(failed)
