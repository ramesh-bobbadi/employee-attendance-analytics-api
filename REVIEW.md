# REVIEW.md

Review of the starter `app/main.py` (line numbers refer to the starter as committed). Every defect below was
reproduced against the unmodified starter first: either by a request to a running server (seeded with the nine
sample documents, run once on this IST machine and once with `TZ=UTC0` to simulate a UTC host), by calling the
helper directly, or on a 100,000-record dataset. Each was then re-run against the fixed code, where it passes.
After the fix, the full suite (25 endpoint checks, 27 helper checks) passes under both IST and UTC host
timezones, and the server logs no tracebacks.

## Helpers

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes`, L37 | The grace test is on whole minutes (`minutes > 10`), but R2 says late means *strictly more than 10:00*. Any punch-in from 10:01 to 10:59 after shift start is reported on time. | Shift 09:30, punch-in 09:40:30 IST: starter returns `0`, expected `10`. Through `POST /attendance/punch-in` the record is stored with `late_minutes: 0`. | Compare whole **seconds** (`seconds > 600`), then return `seconds // 60`. Re-checked 09:40:00 → 0, 09:40:01 → 10, 10:15:59 → 45. |
| 2 | `compute_late_minutes`, L35 | It builds shift start with `punch_in.replace(hour, minute)`, so the start is in *whatever zone `punch_in` is in*, not IST. It also always uses `punch_in`'s calendar day, so overnight shifts are wrong. | 10:05 IST passed as a UTC-aware datetime (what a tz-aware client returns): got `0`, expected `35`. Overnight shift 22:00–06:00, punch-in 01:00 IST: got `0`, expected `180`. | New signature `compute_late_minutes(punch_in, shift_start, date_str)`. Shift start is built as an IST datetime on the record's attendance date, so it works for any input zone and for overnight shifts. |
| 3 | `compute_work_hours`, L41 | Uses Python `round()` on a float. That is neither half-up (R4) nor exact, and instants are not truncated to whole seconds first (R1). | 3618 s (exactly 1.005 h): got `1.0`, expected `1.01`. | Truncate both instants to whole seconds, divide with `Decimal`, then `quantize(ROUND_HALF_UP)`. Re-checked 1.005 → 1.01, 4.495 → 4.5, 4.49497 → 4.49, and punch-in .900 s + 18 s → 0.01. |
| 4 | `compute_overtime`, L47 | No 30-minute minimum (R3): any positive overtime is returned. | Shift end 18:30, punch-out 18:35: got `5`, expected `0` (this is sample document 1). | Return the floor minutes only when `>= 30`, otherwise 0. Re-checked 29:59 → 0, 30:00 → 30, 40:59 → 40, before shift end → 0. |
| 5 | `compute_overtime`, L46 | For an overnight shift (`shift_end <= shift_start`), shift end is placed on the attendance date instead of the next day. | Sample document 7 (22:00–06:00, out 06:40 next day): got `1480`, expected `40`. | Pass `shift_start` too. When the shift is overnight, shift end is the next calendar day. |

**Helper reuse decision:** none of the three helpers is safe to reuse as-is. I fixed them in place (same names,
same role) rather than replacing them, because punch-out and PATCH need exactly these three calculations. The
signatures changed where the old ones could not carry enough information: late needs the attendance date,
overtime needs `shift_start` to detect an overnight shift. Two new helpers, `attendance_date` (R1) and
`round_half_up` (R4/R8), sit next to them.

## Configuration / startup

| # | Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 6 | `MongoClient(...)`, L22 | No `tz_aware=True`, so PyMongo returns **naive** datetimes, while every correct calculation needs aware ones. | Reading sample document 2 back gives `punch_out = datetime(2026,7,6,13,40)` with no zone. Passing it to `compute_overtime` raises `TypeError: can't subtract offset-naive and offset-aware datetimes`, which would be a 500 in punch-out or PATCH. | `MongoClient(..., tz_aware=True)`. Every instant inside the app is now a UTC-aware datetime; epoch ms exist only at the HTTP boundary (`from_millis` / `to_millis`). The same stored value now gives 40. |
| 7 | module level (nothing) | No indexes are created at startup: no uniqueness guarantee on `employees.emp_code` or `(emp_code, date)`, and nothing backs the list sort. This is the root cause of #9, #19 and #23. | `index_information()` on a fresh DB shows only `_id_`. | `ensure_indexes()` runs in the FastAPI lifespan, idempotently: unique `emp_code`, `(department, emp_code)`, unique `(emp_code, date)`, `(date desc, emp_code asc)`. If MongoDB is down at startup it is retried from `/health` and before every insert. |

## Endpoints

| # | Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 8 | `health`, L72–74 | Always returns 200 without contacting MongoDB. The contract requires 503 when MongoDB does not answer. | Started the app with `MONGO_URI=mongodb://127.0.0.1:27999`: `/health` returned **200**. | `ping` MongoDB and return 503 `{"detail": ...}` on any `PyMongoError`. `serverSelectionTimeoutMS=3000`, so the 503 comes back in about 3 s instead of after PyMongo's 30 s default. |
| 9 | `create_employee`, L79–83 | Check-then-insert (`find_one`, then `insert_one`) is not atomic, and no unique index exists. | 10 parallel identical `POST /employees`: **3 × 201**, 3 documents with the same `emp_code`. | Insert directly; the unique `emp_code` index rejects duplicates, and `DuplicateKeyError` → 409. Re-run: exactly 1 × 201, 9 × 409, 1 document. |
| 10 | `create_employee`, L82 (and the `GET /employees` response) | `datetime.now()` is naive *local* time, which PyMongo stores as if it were UTC. The response then returns a datetime string, not epoch ms. | On this IST host the stored `created_at` is **5h30m in the future**. The response contains `"2026-10-11T06:59:14.336591"` instead of an integer, and `GET /employees` returns `"2026-07-13T04:00:00"`. | `datetime.now(timezone.utc)` truncated to ms (what BSON keeps). Responses go through `employee_out()`, which converts it to epoch ms. Re-run: drift 0 s; sample `created_at` → `1767587400000`. |
| 11 | `EmployeeIn`, L54–61 | No validation at all: emp_code pattern, email, `HH:MM`, a real `joined_on` date, `shift_start != shift_end`, and string lengths are all unchecked. | `emp_code:"bad"`, `email:"not-an-email"`, `shift_start:"25:00"`, `joined_on:"2026-13-45"`, `shift_start == shift_end`, `name:""`: all **201**. | Constraints from `EmployeeCreate` (`[0-9]` rather than `\d`, because pydantic's regex treats `\d` as any Unicode digit). `joined_on` must parse as a real date. A model validator rejects equal shift times. All six cases now return 422. |
| 12 | `list_employees`, L93 | `skip = page * page_size` skips the first page. | `?page=1&page_size=2` returned `EMP0003, EMP0004`; with ≤ 20 employees, page 1 is empty. | `skip((page - 1) * page_size)`. |
| 13 | `list_employees`, L94 | `total` counts the whole collection, ignoring the filter (R10). | `?department=Sales`: `total=16` (the whole collection), expected `2`. | `count_documents(q)`. |
| 14 | `list_employees`, L95 | No sort, so results come back in natural order. | Inserting `EMP0000` after the others: it is not listed first. | `.sort("emp_code", 1)`, backed by the `emp_code` / `(department, emp_code)` indexes. |
| 15 | `list_employees`, L89 (and `list_attendance`, L130–131) | `page` and `page_size` are unbounded. | `page=-1` → **500** (negative skip); `page_size=0` → unlimited result; `page_size=101` accepted; `page=0` → 200. | `Query(ge=1)` / `Query(ge=1, le=100)` → 422. |
| 16 | `punch_in`, L101/113 | No check that the employee exists; `emp["shift_start"]` on `None` crashes. | Punch-in for `EMP9999` → **500**. | 404 `employee not found`. |
| 17 | `punch_in`, L102–103 | `datetime.fromtimestamp(ms / 1000)` uses the **server's** local zone. On a UTC host `date` is the UTC date, not IST (R1). On an IST host the date is right, but the naive local time is stored as if it were UTC, so the stored instant is 5h30m off. | `punched_at` = 2026-07-20 01:00 IST. With `TZ=UTC0`: `date` = `2026-07-19` (wrong). On the IST host: stored `punch_in` = `2026-07-20T01:00Z` instead of `2026-07-19T19:30Z`. | `from_millis()` builds a UTC-aware datetime. `attendance_date()` converts it to IST for the calendar date. Re-run under both host zones: `date=2026-07-20`, and the stored instant is exact. |
| 18 | `punch_in`, L102 | Instants are not truncated to whole seconds (R1). | `punched_at` with 900 ms: stored and returned with `.900`. | `from_millis()` floors to whole seconds before anything else; "now" is truncated the same way. |
| 19 | `punch_in`, L104–118 | Check-then-insert race, no unique `(emp_code, date)` index. | 10 parallel identical punch-ins: **3 × 201**, 3 records for one day. | Insert directly; the unique `(emp_code, date)` index decides the race, and `DuplicateKeyError` → 409. Re-run: exactly 1 × 201, 9 × 409, 1 document. |
| 20 | `punch_in`, L103 | Overnight shifts are ignored for the attendance date (R1). | Shift 22:00–06:00, punch-in 01:00 IST on 07-21: `date=2026-07-21`, `late_minutes=0`. Expected `2026-07-20` and `180`. | `attendance_date()` moves a punch-in earlier than `shift_end` to the previous day when the shift is overnight. Checked 05:59:59 → previous day, 06:00:00 → same day. |
| 21 | `PunchInIn`, L64–67 | `status` is a free string, so `ABSENT`, `LEAVE` and garbage are accepted. `punched_at` is a lax `int`, so seconds, floats with `.0`, numeric strings, out-of-range values and `0` are accepted. | `status: ABSENT/LEAVE/FOO` → **201**. `punched_at: 1783312500` (seconds) → 201 with a 1970 date. `1783312500000.0` → 201. `4102444800001` → 201. `0` → 201 (and `if body.punched_at` treated it as "now"). | `status: Literal["PRESENT","WFH","ON_DUTY"]`. `punched_at`: strict int within `[100000000000, 4102444800000]`; `is not None` instead of truthiness. All now return 422. |
| 22 | `punch_in`, L119–121 | The response is not an `AttendanceRecord`: it adds an `id` (records have no id), and instants are datetime strings, not epoch ms. | Response keys include `id`; `punch_in` = `"2026-07-27T09:30:00"`. | Shared `record_out()` serializer returns exactly the contract fields, with instants as epoch ms. |
| 23 | `list_attendance`, L144–147 | Loads **every matching document** into Python, then sorts and slices in memory. With no index, MongoDB collection-scans. | 100,000 records: `GET /attendance?page_size=20` takes **1303 ms**; the plan is `SORT` + `COLLSCAN`, 100,000 docs examined. | Filter, sort, skip and limit run in MongoDB; `total = count_documents(q)`. Re-run: **66 ms**. `IXSCAN` with 20 keys / 20 docs examined, both unfiltered and with an `emp_code` or date-range filter. |
| 24 | `list_attendance`, L145 | Sorts by `date` only; the contract requires `date` descending, **then `emp_code` ascending**. | Records for 2026-07-06 come back as `EMP0001, EMP0003, EMP0005, EMP0002` (insertion order). | `sort([("date", -1), ("emp_code", 1)])`, matched by the `(date desc, emp_code asc)` index. |
| 25 | `list_attendance`, L148–150 | Leaks `id`, returns instants (including `history[].at` and `history[].changes.punch_in.from/to`) as datetime strings, and returns legacy documents without `history` / `half_day`. | The sample legacy record (EMP0002, 2026-07-14) has no `history` / `half_day` keys. Sample record 8 returns `history[0].at = "2026-07-08T05:00:00"`. | `record_out()`: epoch ms everywhere, including inside `history`. Missing `history` → `[]`, `half_day` → `false`, `late_minutes` / `overtime_minutes` → `0` (all per DATA_MODEL.md). `work_hours` is left `null` when absent; no default is invented. |
| 26 | `list_attendance`, L127–129 | No validation of query params: `date_from > date_to`, malformed dates, and unknown `status` values are accepted. | `date_from=2026-07-10&date_to=2026-07-01`, `date_from=garbage`, `status=FOO`: all **200** with empty results. | `YYYY-MM-DD` pattern plus a real-date check (so `2026-02-30` is a 422). `status` is the `Status` enum. `date_from > date_to` raises `RequestValidationError`, so the body is FastAPI's default `{"detail": [...]}`. |

## Examined and judged not a defect

- **`load_dotenv()` (L20)**: it does not override variables already in the environment, so real env vars win as
  required. Verified: the `/health` 503 test sets `MONGO_URI` in the process env while `.env` points at a live
  server, and the env value was used.
- **`os.getenv("MONGO_URI", "mongodb://localhost:27017")` / `MONGO_DB` default (L22–23)**: local-dev fallbacks
  only; injected values always take precedence. Kept.
- **`list_attendance` filter construction (L133–143)**: string comparison on `YYYY-MM-DD` is correct for
  inclusive date ranges. Kept as-is and moved into `attendance_list_query()` so the explain endpoint can build the
  same query later.
- **`doc.pop("_id")` after `insert_one` (L84, L120)**: correct, since `insert_one` mutates the dict. It is now
  unnecessary, because the serializers whitelist fields.
- **The punch-in document's initial values (L106–117)**: `punch_out`/`work_hours` null, `overtime_minutes` 0,
  `half_day` false, `history` [] match the DATA_MODEL "no punch_out yet" row. Kept.
- **The `IST` constant (L25)**: correct (`+05:30`). Kept.
- **`PunchInIn.punched_at` accepting explicit `null` as "now"**: the contract only says "omitted → now". Accepting
  `null` the same way is lenient but not contradictory, so it was left as is.
