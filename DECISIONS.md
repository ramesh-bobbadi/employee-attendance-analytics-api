# DECISIONS.md

1. **Indexes.** On `attendance_logs`: unique `(emp_code, date)`, one record per day and the `$lookup` for monthly, summary
   and trend; `(date desc, emp_code)`, the attendance list's sort and range and the leaderboard's month filter. On
   `employees`: unique `emp_code` (create race, leaderboard join), `(department, emp_code)` (department filters) and
   `joined_on` (summary headcount). Created idempotently at startup; on 100k records every explained plan is IXSCAN, never
   COLLSCAN. The unfiltered attendance explain also uses `date_desc_emp_code` (20 keys and 20 documents examined).
   The earlier 507.627 ms p99 outlier was not reproduced in a 500-request rerun: p50 91.446 ms, p95 124.499 ms,
   p99 143.967 ms, with zero errors. Rejected: `(status, date, emp_code)`. A status-only filter still uses the date
   index (worst case 167 ms on 100k), and it would slow every write.
   Note: the original 507.627ms p99 was measured on a 50-request sample, too small for a reliable percentile — a single slow outlier can dominate at that size. The 500-request re-run shown above is the statistically meaningful result.

2. **Punch-in race.** Both call `insert_one` with the same document. The unique `(emp_code, date)` index accepts one; the
   other raises `DuplicateKeyError`, returned as 409. Ten parallel requests gave one 201, nine 409s and one document.

3. **Ties.** `$rank` gives competition ranks (1, 2, 2, 4); then I keep `rank <= limit`. With limit 2 and a tie at rank 2,
   all three rows come back, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` who joined by month end. `$lookup` gives a zero-log employee
   `logs: []`, and `$group` still counts them once.

5. **100x.** Department summary reads every log of the month (30k here, 194 ms). I would keep per-employee monthly totals,
   updated by each write.
