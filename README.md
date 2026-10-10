# Employee Attendance & Analytics API

A high-performance Python FastAPI service backed by MongoDB for employee management, punch-in/out tracking, manual attendance corrections with audit trail, and analytics pipelines.

---

## Features Implemented

1. **Employee Management (`/employees`)**:
   - Create employees with customer-assigned unique `emp_code`s.
   - List employees with department filtering and deterministic sorting.
2. **Attendance Operations (`/attendance`)**:
   - `POST /attendance/punch-in`: Record punch-ins with automated IST date derivation (including overnight shifts `shift_end <= shift_start`), late arrival calculation (10-minute grace period), and atomic unique constraint protection.
   - `POST /attendance/punch-out`: Close open punch-in records, calculate work hours (half-up rounding), overtime minutes, and half-day status.
   - `GET /attendance`: Paginated listing of attendance logs sorted by `date` descending and `emp_code` ascending.
   - `PATCH /attendance/{emp_code}/{date}`: Regularize attendance records with automatic re-computation of derived fields and audit history logging.
3. **Analytics & Aggregation (`/analytics`)**:
   - `GET /analytics/employees/{emp_code}/monthly`: Monthly summary of working days, present days (half day = 0.5), leave days, late count, total overtime, and attendance percentage.
   - `GET /analytics/departments/summary`: Department-level summary across all employees, preserving zero-log employee headcounts.
   - `GET /analytics/leaderboard/late`: Standard competition ranking (1, 2, 2, 4) for late arrivals with post-rank limit evaluation.
   - `GET /analytics/departments/{department}/trend`: Daily attendance trend with MongoDB `$densify` gap filling and 7-day moving averages.
4. **Admin & Diagnostics (`/admin`)**:
   - `GET /admin/explain/{endpoint}`: MongoDB `executionStats` query explain plans verifying `IXSCAN` usage.

---

## Local Setup Instructions

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ (local Docker container or MongoDB Atlas cluster)

### 1. Environment & Dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment Variables Configuration
Create a `.env` file in the project root:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 3. Seed Sample Data
```bash
python sample_seed.py
```

### 4. Run the Application
```bash
uvicorn app.main:app --port 8000 --reload
```

---

## API Documentation & Testing

- **Swagger UI**: Interactive API documentation is available at `http://localhost:8000/docs`.
- **ReDoc**: Alternative documentation is available at `http://localhost:8000/redoc`.

### Run Test Suite
```bash
python -m pytest test_app.py -v
```

---

## Database Indexing Strategy

Indexes are created automatically at application startup:
- `employees.emp_code` (Unique)
- `employees.department`
- `attendance_logs.(emp_code, date)` (Unique Compound)
- `attendance_logs.(date, status)`
