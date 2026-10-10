# DECISIONS.md

## 1. Indexes
Created indexes:
- `employees`: `{ emp_code: 1 }` (Unique) — Guarantees customer `emp_code` uniqueness and accelerates employee lookups/joins.
- `employees`: `{ department: 1 }`, `{ joined_on: 1 }` — Optimizes department filtering and headcount aggregations.
- `attendance_logs`: `{ emp_code: 1, date: 1 }` (Unique) — Guarantees at most one attendance record per employee per attendance date, enforcing punch-in race protection.
- `attendance_logs`: `{ date: -1, emp_code: 1 }` — Enables fast paginated sorting for `GET /attendance` without in-memory sorting.
- `attendance_logs`: `{ date: 1, status: 1 }` — Accelerates monthly analytics and leaderboard filtering.

*Rejected Index*: Single-field index on `attendance_logs.punch_in`. Rejected because queries address logs by attendance date (`date` string in IST) and `emp_code`, not raw UTC timestamps.

## 2. Punch-in Race Condition
When two identical punch-in requests for the same employee and date arrive simultaneously:
1. Both requests parse `punched_at` and derive the attendance date (`YYYY-MM-DD`).
2. Both attempt `db.attendance_logs.insert_one(doc)`.
3. MongoDB's unique compound index on `(emp_code, date)` enforces atomic insertion at the storage engine layer (WiredTiger).
4. Exactly one request succeeds and returns `201 Created`.
5. The concurrent request fails with a PyMongo `DuplicateKeyError`, caught by FastAPI to return `409 Conflict` (`"already punched in for this date"`).

## 3. Leaderboard Ties
Ties are handled using **Standard Competition Ranking (1, 2, 2, 4)** as mandated by the spec:
- Employees with identical `total_late_minutes` share the same `rank`.
- The next rank skips by the number of tied positions.
- The `limit` parameter is applied **after** rank calculation: all employees with `rank <= limit` are returned.
- If two employees tie at `rank = 10` for `limit = 10`, both are included, yielding 11 rows. Secondary sorting (`emp_code` ascending) ensures deterministic output ordering.

## 4. Headcount Calculation
Department headcount per R9 includes all employees whose `joined_on` date is on or before the period end date, even if they have zero attendance records:
- The aggregation pipeline starts with a `$match` on `employees` (`joined_on <= period_end_date`).
- It performs a `$lookup` to `attendance_logs`.
- By setting `preserveNullAndEmptyArrays: True` on `$unwind`, employees with no matching logs remain in the pipeline stream.
- Headcount is calculated as the count of distinct eligible `emp_code`s per department.

## 5. Scaling to 100x Data Volume
If data scales 100x (~10 million attendance records):
- **Sharding**: Shard the `attendance_logs` collection by `emp_code` (ranged/hashed) to distribute read/write load across MongoDB nodes.
- **Pre-aggregated Rollups**: Store daily/monthly metric summaries in a dedicated `monthly_stats` collection updated asynchronously via change streams, eliminating real-time aggregation across millions of raw logs.
