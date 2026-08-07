import os
import sys
from datetime import date

# backend/tests/ -> backend/, so `app` imports regardless of where this is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.window import HistoryWindowExtractor

AS_OF = date(2026, 8, 6)
X = HistoryWindowExtractor()

CASES = [
    # (label, sql, expected_earliest or None for unbounded)
    ("literal lower bound",
     "SELECT * FROM patient_encounters WHERE event_date >= DATE '2025-08-01'",
     date(2025, 8, 1)),

    ("relative interval days",
     "SELECT count(*) FROM patient_encounters WHERE event_date >= CURRENT_DATE - INTERVAL '90 days'",
     date(2026, 5, 8)),

    ("BETWEEN",
     "SELECT * FROM patient_encounters WHERE event_date BETWEEN DATE '2024-01-01' AND CURRENT_DATE",
     date(2024, 1, 1)),

    ("function wrapped cast",
     "SELECT * FROM patient_encounters WHERE event_date > (NOW() - INTERVAL '24 months')::date",
     date(2024, 8, 6)),

    ("date_trunc both sides",
     "SELECT date_trunc('month', event_date) m, count(*) FROM patient_encounters "
     "WHERE date_trunc('month', event_date) >= date_trunc('month', CURRENT_DATE - INTERVAL '18 months') GROUP BY 1",
     date(2025, 2, 1)),

    ("NO predicate -> unbounded (the killer case)",
     "SELECT patient_id, result_value FROM lab_results",
     None),

    ("predicate on a different column -> unbounded",
     "SELECT * FROM patient_encounters WHERE region = 'WEST'",
     None),

    ("AND takes the LATEST bound",
     "SELECT * FROM patient_encounters WHERE event_date >= DATE '2024-01-01' AND event_date >= DATE '2025-06-01'",
     date(2025, 6, 1)),

    ("OR takes the EARLIEST bound",
     "SELECT * FROM patient_encounters WHERE event_date >= DATE '2024-01-01' OR event_date >= DATE '2025-06-01'",
     date(2024, 1, 1)),

    ("OR with an unconstrained branch -> unbounded",
     "SELECT * FROM patient_encounters WHERE event_date >= DATE '2025-01-01' OR region = 'WEST'",
     None),

    ("alias qualified",
     "SELECT pe.* FROM patient_encounters pe WHERE pe.event_date >= DATE '2025-03-15'",
     date(2025, 3, 15)),

    ("schema qualified table",
     "SELECT * FROM public.patient_encounters WHERE event_date >= DATE '2025-04-01'",
     date(2025, 4, 1)),

    ("reversed operands",
     "SELECT * FROM patient_encounters WHERE DATE '2025-02-01' <= event_date",
     date(2025, 2, 1)),

    ("join with date filter on subject",
     "SELECT * FROM patient_encounters pe JOIN dim_patient d ON d.patient_id = pe.patient_id "
     "WHERE pe.event_date >= CURRENT_DATE - INTERVAL '12 months'",
     date(2025, 8, 6)),

    ("query does not reference subject",
     "SELECT * FROM some_other_table WHERE event_date >= DATE '2025-01-01'",
     None),

    ("NOT -> unbounded",
     "SELECT * FROM patient_encounters WHERE NOT (event_date < DATE '2025-01-01')",
     None),

    ("numeric day arithmetic",
     "SELECT * FROM patient_encounters WHERE event_date >= CURRENT_DATE - 30",
     date(2026, 7, 7)),

    ("year truncation",
     "SELECT * FROM patient_encounters WHERE event_date >= date_trunc('year', CURRENT_DATE - INTERVAL '2 years')",
     date(2024, 1, 1)),

    ("unparseable garbage -> unbounded",
     "SELEKT ***(( FROM patient_encounters WHERE",
     None),

    ("subquery/CTE without provable outer filter -> unbounded",
     "WITH raw AS (SELECT * FROM patient_encounters) SELECT * FROM raw WHERE event_date >= DATE '2025-01-01'",
     None),
]

passed = failed = 0
for label, sql, expected in CASES:
    got = X.extract(sql, "patient_encounters" if "lab_results" not in sql else "lab_results",
                    "event_date", as_of=AS_OF)
    ok = got.earliest == expected
    status = "PASS" if ok else "FAIL"
    if ok: passed += 1
    else: failed += 1
    print(f"[{status}] {label}")
    print(f"        expected={expected}  got={got.earliest}")
    if got.predicate_text:
        print(f"        predicate: {got.predicate_text}")
    if not ok:
        print(f"        note: {got.note}")
    print()

print(f"=== {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
