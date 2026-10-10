# REVIEW.md

Comprehensive defect review of the starter `app/main.py` implementation.

## Defects Found and Fixed

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (lines 33-37) | Hardcoded naive string parsing; fails on overnight shifts; checks `minutes > 10` returning raw diff without floor minutes or IST timezone context. | Punch-in at 09:40:01 IST returned 10 while midnight shift punch-ins yielded invalid/negative numbers. | Rewrote using IST timezone conversion (`Asia/Kolkata`), checking strictly $>10$ minutes (600s) grace period, calculating floor elapsed minutes since scheduled shift start on attendance date. |
| 2 | `compute_work_hours` (line 40-41) | Uses Python's native `round()`, which applies Banker's Rounding (`ROUND_HALF_EVEN`) instead of `ROUND_HALF_UP` required by R4/R8. | A duration resulting in `4.495` hours rounds to `4.49` instead of `4.50`, incorrectly classifying it as a half-day. | Used Python's `decimal` module with `ROUND_HALF_UP` quantization to exactly two decimal places. |
| 3 | `compute_overtime` (lines 44-47) | Parses date string directly using `datetime.fromisoformat` without IST offset or overnight shift logic (`shift_end <= shift_start`). | Overnight shift punch-outs (e.g. shift 22:00-06:00, punch-out at 06:40 next day) calculated negative or absurd overtime. | Calculated shift end on the next calendar day for overnight shifts in IST, ensuring overtime is counted only when $\ge 30$ minutes. |
| 4 | `create_employee` (lines 78-86) | Vulnerable to check-then-insert race condition (`find_one` then `insert_one`) and omits instant creation time formatting. | Two concurrent `POST /employees` requests with identical `emp_code` both pass `find_one` and create duplicate documents. | Added database unique index on `emp_code` and caught PyMongo `DuplicateKeyError` to return HTTP 409 atomically. |
| 5 | `list_employees` (line 93) | Calculates pagination offset as `skip = page * page_size` instead of `(page - 1) * page_size`, skipping the first page of results entirely. | Requesting `page=1` with `page_size=20` returns items 21-40 instead of 1-20. | Corrected skip calculation to `(page - 1) * page_size` and added `.sort("emp_code", 1)` for deterministic ordering. |
| 6 | `punch_in` (lines 100-121) | Uses `find_one` followed by `insert_one` (race condition) and ignores overnight shift attendance date derivation (R1). | Concurrent punch-ins for the same day create duplicate logs; overnight shift punch-in at 01:10 AM gets assigned to the wrong date. | Implemented `get_attendance_date` helper for R1 overnight shifts, created a unique compound index on `(emp_code, date)`, and handled `DuplicateKeyError` to return 409 atomically. |
| 7 | `list_attendance` (lines 144-150) | Loads all matching documents into memory (`list(...)`), sorts and slices in Python instead of using MongoDB query cursor operations. | Memory exhaustion and slow execution on datasets with ~100,000 records. | Used native MongoDB `.sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)` on indexed fields. |
| 8 | Entire starter file | Missing 7 required contract endpoints: `punch-out`, `regularize`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`, and `admin/explain`. | Automated contract validation suite fails with 404 on all missing endpoints. | Implemented all missing endpoints according to `openapi.yaml` and R1-R10 rules. |

## Evaluated and Not Defective

1. **`app.get("/health")` endpoint**:
   - *Checked*: Simple dictionary return `{"status": "ok"}`.
   - *Conclusion*: Updated to ping MongoDB (`client.admin.command('ping')`) and return HTTP 503 if unreachable as specified by `openapi.yaml`.

2. **Use of Pydantic BaseModel for Request Payload Validation**:
   - *Checked*: Pydantic model schemas for `EmployeeIn` / `PunchInIn`.
   - *Conclusion*: Not defective in principle, but schemas were expanded to strictly validate epoch ms ranges, email formats, regex pattern constraints, and shift strings.
