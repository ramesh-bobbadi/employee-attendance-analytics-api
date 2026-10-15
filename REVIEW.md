# REVIEW.md

The review is based on the original starter version of app/main.py(the file before any edits).

I tested each issue in the original code first to confirm that the problem was actually happening. After confirming the problem, I fixed the code and tested it again to make sure the fix worked.

I tested each issue by :

- sending requests to the running API with the nine sample documents
- calling the helper function directly
- checking the behavior under both IST and UTC host time zones
- testing the performance with 100,000 attendance records

## Helpers

### compute_late_minutes [Line 37] :

`Issue` = The starter code checks the lateness using the whole minutes (minutes > 10). However, according to the R2 rule, an employee is late when the punch-in time is strictly more than 10 minutes after the shift starts. Because of this, some employees who were slightly more than 10 minutes late were incorrectly marked as on time.

`How I noticed it` = For a shift starting at 09:30, a punch-in at 09:40:30 returned 0 late minutes instead of 10. The same issue appeared through POST /attendance/punch-in, where the record was stored with late_minutes = 0.

`How I fixed it `= I changed the check to compare seconds (seconds > 600) and then calculate the late minutes using seconds // 60. I verified that 09:40:00 returns 0, 09:40:01 returns 10, and 10:15:59 returns 45.
 
### compute_late_minutes [Line 35] :

`Issue` = The starter code creates the shift start using the timezone of the punch_in value instead of IST. It also always uses the punch_in calendar date, which produces incorrect results for overnight shifts.

`How I noticed it` = A punch-in at 10:05 IST, passed as a UTC-aware datetime, returned 0 instead of the expected 35 late minutes. For an overnight shift from 22:00 to 06:00, a punch-in at 01:00 IST on 07-21 returned 0 instead of the expected 180 late minutes.

`How I fixed it` = I changed the function signature to receive the punch_in, shift_start, and date_str. The shift start is now created as an IST datetime using the attendance date. This makes the calculation work correctly for different input time zones and overnight shifts.

### compute_work_hours [Line 41] :

`Issue` = The starter code uses Python's round() function with a floating-point value. This does not follow the required half-up rounding rule and may also include fractional seconds that should be truncated first.

`How I noticed it` = A duration of 3,618 seconds, which is exactly 1.005 hours, returned 1.0 instead of 1.01.

`How I fixed it` = I truncated both timestamps to whole seconds, calculated the duration using Decimal, and applied ROUND_HALF_UP. I verified values such as 1.005 returned 1.01 and 4.495 returned 4.50.

### compute_overtime [Line 47] : 

`Issue` = The starter code counts any positive overtime, but the R3 rule says overtime should be counted only when it reaches at least 30 minutes.

`How I noticed it` = For a shift ending at 18:30 and a punch-out at 18:35, the starter returned 5 overtime minutes. the expected result was 0.

`How I fixed it` = I changed the function to return the calculated whole minutes only when the overtime is at least 30 minutes. Otherwise, it returns 0. I verified that 18:59:59 returns 0, while 19:00:00 returns 30.

### compute_overtime [Line 46] : 

`Issue` = For an overnight shift, the starter code places the shift end on the attendance date instead of the following day.

`How I noticed it` = For a shift from 22:00 to 06:00 with a punch-out at 06:40 the next day, the starter returned 1,480 overtime minutes instead of the expected 40.

`How I fixed it` = I passed the shift start to the helper so it can identify overnight shifts. When the shift is overnight, the shift end is now placed on the next calendar day.

### Helpers reuse decision 

- None of the three original helpers was safe to reuse without changes. I fixed them in place of instead of replacing them because both the punch-out and regularization operations use these calculations.

### Added two helpers

- `attendance_date` to handle IST attendance dates and overnight shifts
- `round_half_up` to apply the required rounding rules.

## Configuration / startup

### MongoClient(...) [Line 22] :

`Issue` = The starter application created the MongoDB client without tz_aware=True. Because of this, dates read from MongoDB were timezone-naive, even though the application’s calculations require timezone-aware datetimes.

`How I noticed it` = When the second sample document was read, its punch_out value was returned as datetime(2026, 7, 6, 13, 40) without timezone information. Passing this value to compute_overtime caused the error TypeError: can't subtract offset-naive and offset-aware datetimes. This could result in a 500 response from the punch-out or regularization endpoint.

`How I fixed it` = I created the MongoDB client with tz_aware=True. Dates read from MongoDB are now UTC-aware, and all internal calculations use timezone-aware datetimes consistently. Epoch milliseconds are converted only at the API boundary. The same stored value now calculates 40 minutes correctly.

### Module level [No existing implementation] :

`Issue` = The starter application did not create the required MongoDB indexes at startup. A new database had only the default _id_ index. This meant there was no uniqueness protection for employee codes or attendance records, and no suitable index for the required list sorting.

`How I noticed it` = I checked the indexes on a fresh database using index_information(), which showed only _id_. Without the unique indexes, concurrent requests could create duplicate employees or duplicate attendance records. Queries also had to scan the collection instead of using an index.

`How I fixed it` = I added an idempotent ensure_indexes() function that creates the unique emp_code index, the (department, emp_code) index, the unique (emp_code, date) index, the (date descending, emp_code ascending) index, and the joined_on index. The function runs during application startup and is retried when MongoDB becomes available.

## Endpoints

### `health` [Lines 72–74]

`Issue` = The starter always returned `200` without checking MongoDB. The contract requires `503` when MongoDB is unavailable.

`How I noticed it` = I started the app with `MONGO_URI=mongodb://127.0.0.1:27999`. The `/health` endpoint still returned `200`.

`How I fixed it:` = I added a MongoDB `ping` and return `503` for `PyMongoError`. I also set `serverSelectionTimeoutMS=3000`, so the response does not wait for the default 30-second timeout.

### `create_employee` [Lines 79–83]

`Issue found` = The starter used a separate `find_one` and `insert_one`. Two simultaneous requests could both pass the check and create duplicate employees.

`How I noticed it:` = Ten identical `POST /employees` requests produced three `201` responses and three documents with the same `emp_code`.

`How I fixed it:` = I inserted directly and added a unique `emp_code` index. `DuplicateKeyError` is converted to `409`. After the fix, one request returns `201`, the other nine return `409`, and only one document is created.

### `create_employee` and `GET /employees` [Line 82]

`Issue found` = `datetime.now()` created a naive local time, which MongoDB stored as UTC. The API also returned datetime strings instead of epoch milliseconds.

`How I noticed it:` = On the IST host, the stored `created_at` was 5 hours and 30 minutes ahead. Responses contained values such as `"2026-10-11T06:59:14.336591"` instead of an integer.

`How I fixed it:` = I used `datetime.now(timezone.utc)`, truncated it to milliseconds, and converted it to epoch milliseconds in `employee_out()`. The corrected sample value was `1767587400000`.

### `EmployeeIn` [Lines 54–61]

`Issue found` = The starter did not validate employee codes, email addresses, shift times, dates, equal shift times, or string lengths.

`How I noticed it:` = Invalid values such as `emp_code:"bad"`, `email:"not-an-email"`, `shift_start:"25:00"`, `joined_on:"2026-13-45"`, equal shift times, and an empty name all returned `201`.

`How I fixed it:` = I added validation for the employee-code pattern, email, string lengths, `HH:MM` shift times, and real dates. A model validator rejects equal shift times. These cases now return `422`.

### `list_employees` [Lines 89–95]

`Issue found` = The starter skipped the first page, calculated `total` without applying filters, and returned employees in natural rather than `emp_code` order.

`How I noticed it:` = `?page=1&page_size=2` returned `EMP0003, EMP0004`; `department=Sales` returned `total=16` when the filtered count was 2; and inserting `EMP0000` did not place it first.

`How I fixed it:` = I used `(page - 1) * page_size` for the offset, `count_documents(q)` for the filtered total, and ascending `emp_code` sorting backed by the employee indexes.

### `list_employees` and `list_attendance` [Lines 89 and 130–131]

`Issue found` = `page` and `page_size` were not bounded.

`How I noticed it:` = `page=-1` caused `500`, `page_size=0` returned an unlimited result, `page_size=101` was accepted, and `page=0` returned `200`.

`How I fixed it:` = I added `page >= 1` and `1 <= page_size <= 100` validation. Invalid values now return `422`.

### `punch_in` [Lines 101 and 113]

`Issue found` = The starter did not check whether the employee existed, so accessing `shift_start` on a missing employee caused an exception.

`How I noticed it:` = Punching in with `EMP9999` returned `500`.

`How I fixed it:` = I added the employee existence check and return `404 employee not found` when the employee is missing.

### `punch_in` [Lines 102–103]

`Issue found` = `datetime.fromtimestamp(ms / 1000)` used the server's local timezone. The result changed between UTC and IST hosts, and the stored instant could be shifted by 5 hours and 30 minutes.

`How I noticed it:` = For `2026-07-20 01:00 IST`, a UTC host stored the date as `2026-07-19`; an IST host stored the instant as `2026-07-20T01:00Z` instead of `2026-07-19T19:30Z`.

`How I fixed it:` = `from_millis()` now creates a UTC-aware datetime, and `attendance_date()` converts it to IST. The date and stored instant are now correct on both host timezones.

### `punch_in` [Line 102]

`Issue found` = The starter did not truncate instants to whole seconds as required by R1.

`How I noticed it:` = A timestamp containing 900 milliseconds was stored and returned with `.900`.

`How I fixed it:` = `from_millis()` now truncates timestamps to whole seconds before calculation and storage.

### `punch_in` [Lines 104–118]

`Issue found` = The starter used a check-then-insert race and had no unique `(emp_code, date)` index, so concurrent requests could create duplicate records.

`How I noticed it:` = Ten identical punch-ins produced three `201` responses and three records for one employee and date.

`How I fixed it:` = I added the unique `(emp_code, date)` index and insert directly. `DuplicateKeyError` becomes `409`. After the fix, one request succeeds and the other nine return `409`.

### `punch_in` [Line 103]

`Issue found` = The starter ignored the overnight-shift rule when calculating the attendance date.

`How I noticed it:` = For a `22:00–06:00` shift, a punch-in at `01:00 IST` on July 21 was stored as `2026-07-21` with `late_minutes=0`. It should belong to the previous day, giving `2026-07-20` and `180` late minutes.

`How I fixed it:` = `attendance_date()` now moves a punch-in before an overnight shift's end time to the previous day. I checked the boundaries: `05:59:59` belongs to the previous day and `06:00:00` belongs to the current day.

### `PunchInIn` [Lines 64–67]

`Issue found` = The starter accepted invalid statuses and loosely validated `punched_at`, including seconds, floats, numeric strings, out-of-range values, and zero.

`How I noticed it:` = `ABSENT`, `LEAVE`, and `FOO` returned `201`; `1783312500` produced a 1970 date; and `1783312500000.0`, `4102444800001`, and `0` were also accepted.

`How I fixed it:` = I restricted status to `PRESENT`, `WFH`, and `ON_DUTY`, added strict epoch-millisecond validation, and used `is not None` instead of truthiness. Invalid values now return `422`.

### `punch_in` [Lines 119–121]

`Issue found` = The response included an internal `id` and returned instants as datetime strings instead of epoch milliseconds.

`How I noticed it:` = The response contained `id`, and `punch_in` was returned as `"2026-07-27T09:30:00"`.

`How I fixed it:` = The shared `record_out()` serializer now returns only contract fields and converts all instants to epoch milliseconds.

### `list_attendance` [Lines 144–147]

`Issue found` = The starter loaded all matching documents into Python and sorted and paginated them there, causing a collection scan on large datasets.

`How I noticed it:` = With 100,000 records, the request took 1,303 ms and the plan showed `SORT` plus `COLLSCAN`, examining all 100,000 documents.

`How I fixed it:` = Filtering, sorting, skipping, and limiting now run in MongoDB using the supporting index. The same test took 66 ms and used `IXSCAN`, examining only the page records.

### `list_attendance` [Line 145]

`Issue found`: The starter sorted only by date, but the contract requires date descending and then `emp_code` ascending.

`How I noticed it:` = Records for `2026-07-06` appeared in insertion order instead of employee-code order.

`How I fixed it:` = I changed the sort to `[("date", -1), ("emp_code", 1)]` and added the matching compound index.

### `list_attendance` [Lines 148–150]

`Issue found` = The starter exposed internal IDs, returned datetime strings, and did not apply the required defaults to legacy records missing `history` and `half_day`.

`How I noticed it:` = The legacy `EMP0002` record had no `history` or `half_day`, and another sample record returned `history[].at` as `"2026-07-08T05:00:00"`.

`How I fixed it:` =  `record_out()` removes internal IDs, converts all instants to epoch milliseconds, defaults `history` to `[]`, `half_day` to `false`, and missing `late_minutes` and `overtime_minutes` to `0`. Missing `work_hours` remains `null`.

### `list_attendance` [Lines 127–129]

`Issue found` = The starter accepted malformed dates, reversed date ranges, and unknown status values.

`How I noticed it:` = Requests with `date_from=2026-07-10&date_to=2026-07-01`, `date_from=garbage`, and `status=FOO` all returned `200`.

`How I fixed it:` = I added format and real-date validation, restricted status to the allowed enum, and made reversed ranges return `422`.

## Code I checked and decided not to change

### `load_dotenv()` [Line 20]

`What I checked` = `load_dotenv()` does not override variables that already exist in the environment. This means
the real `MONGO_URI` and `MONGO_DB` values take priority, as required by the contract.

`How I verified it:` = I ran the `/health` test with `MONGO_URI` set in the process environment while `.env`
pointed to a live server. The application used the process environment value.

### MongoDB environment defaults [Lines 22–23]

`What I checked` = The `MONGO_URI` and `MONGO_DB` defaults are only local-development fallbacks. Values injected
through the environment still take priority.

`How I verified it:` = I ran the `/health` test with `MONGO_URI` set in the process environment while `.env`
pointed to a live server. The application used the process environment value.

### `list_attendance` filter construction [Lines 133–143]

`What I checked` = Comparing dates as `YYYY-MM-DD` strings correctly supports inclusive date ranges because the
format sorts chronologically.

`How I verified it:` = I tested various date range scenarios and confirmed that the filtering worked as expected.

### `doc.pop("_id")` after `insert_one` [Lines 84 and 120]

`What I checked` = `insert_one` adds `_id` to the inserted dictionary, so removing it was correct.

`How I verified it:` = I checked that the `_id` field is removed from the document after insertion.

### Initial punch-in values [Lines 106–117]

`What I checked` = A new attendance record starts with punch_out and work_hours set to null,

`overtime_minutes` set to 0, half_day set to false, and an empty history array.

`How I verified it:` = I confirmed that the initial values are correctly set.

### `IST` constant [Line 25]

`What I checked` = The constant uses the correct +05:30 offset.

### `PunchInIn.punched_at` accepting explicit `null`

`What I checked` = The contract says that an omitted `punched_at` uses the current time. Treating an explicit
`null` the same way is permissive but does not contradict the contract.
