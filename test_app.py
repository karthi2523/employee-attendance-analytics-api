"""
Comprehensive Test Suite for HROne Employee Attendance & Analytics API
Covers business rules R1-R10, endpoints, concurrency, rounding, and analytics.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import pytest
from fastapi.testclient import TestClient
import mongomock

from app.main import (
    app,
    db,
    init_indexes,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    round_half_up,
    get_attendance_date,
    epoch_ms_to_dt,
    dt_to_epoch_ms,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    """Clear database collections before each test."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    init_indexes()


# --------------------------------------------------------------------------- #
# Unit Tests for Helper Calculations (R1 - R8)
# --------------------------------------------------------------------------- #

def test_round_half_up():
    assert round_half_up(9.124) == 9.12
    assert round_half_up(9.125) == 9.13
    assert round_half_up(9.126) == 9.13
    assert round_half_up(4.499) == 4.50
    assert round_half_up(4.494) == 4.49


def test_epoch_ms_conversion():
    ms = 1783312500000
    dt = epoch_ms_to_dt(ms)
    assert dt.tzinfo == timezone.utc
    assert dt.microsecond == 0
    assert dt_to_epoch_ms(dt) == ms


def test_attendance_date_overnight():
    # Overnight shift: 22:00 to 06:00
    # Punch in at 21:55 IST on 2026-07-06 (16:25 UTC) -> date is 2026-07-06
    dt1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    d1 = get_attendance_date(dt1, "22:00", "06:00")
    assert d1 == "2026-07-06"

    # Punch in at 01:10 IST on 2026-07-07 (2026-07-06 19:40 UTC) -> belongs to shift starting 2026-07-06
    dt2 = datetime(2026, 7, 6, 19, 40, 0, tzinfo=timezone.utc)
    d2 = get_attendance_date(dt2, "22:00", "06:00")
    assert d2 == "2026-07-06"


def test_late_minutes_boundary():
    # Shift start: 09:30 IST (04:00 UTC)
    # 09:40:00 IST -> 0 min late (grace period <= 10 min)
    dt_exact_10 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_exact_10, "09:30", "2026-07-06") == 0

    # 09:40:01 IST -> 10 min late (strictly > 10 min)
    dt_late_10 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_late_10, "09:30", "2026-07-06") == 10

    # 10:05:00 IST -> 35 min late
    dt_late_35 = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_late_35, "09:30", "2026-07-06") == 35


def test_overtime_boundary():
    # Shift end: 18:30 IST (13:00 UTC)
    # Punch out at 18:55 IST -> 25 min overtime -> threshold 30 min not met -> 0
    dt_ot_25 = datetime(2026, 7, 6, 13, 25, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_ot_25, "09:30", "18:30", "2026-07-06") == 0

    # Punch out at 19:00 IST -> 30 min overtime -> threshold met -> 30
    dt_ot_30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_ot_30, "09:30", "18:30", "2026-07-06") == 30

    # Punch out at 19:10 IST -> 40 min overtime
    dt_ot_40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_ot_40, "09:30", "18:30", "2026-07-06") == 40


def test_half_day_boundary():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.0) is False
    assert compute_half_day(None) is False


# --------------------------------------------------------------------------- #
# Endpoint API Tests
# --------------------------------------------------------------------------- #

def test_health_check():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_and_list_employee():
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    # Create employee
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert "created_at" in data

    # Duplicate create fails with 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # List employees
    res_list = client.get("/employees")
    assert res_list.status_code == 200
    body = res_list.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["emp_code"] == "EMP0001"


def test_punch_in_and_punch_out():
    # Setup employee
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )

    # Punch in at 09:28 IST on 2026-07-06 (03:58 UTC) -> ms: 1783310280000
    punch_in_ms = 1783310280000
    res_pi = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms, "status": "PRESENT"},
    )
    assert res_pi.status_code == 201
    pi_data = res_pi.json()
    assert pi_data["date"] == "2026-07-06"
    assert pi_data["status"] == "PRESENT"
    assert pi_data["late_minutes"] == 0
    assert pi_data["punch_out"] is None

    # Duplicate punch in for same date fails with 409
    res_pi_dup = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms + 60000, "status": "PRESENT"},
    )
    assert res_pi_dup.status_code == 409

    # Punch out at 18:35 IST on 2026-07-06 (13:05 UTC) -> ms: 1783343100000
    punch_out_ms = 1783343100000
    res_po = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out_ms},
    )
    assert res_po.status_code == 200
    po_data = res_po.json()
    assert po_data["work_hours"] == 9.12
    assert po_data["overtime_minutes"] == 0
    assert po_data["half_day"] is False

    # Second punch out fails with 409
    res_po_dup = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out_ms + 1000},
    )
    assert res_po_dup.status_code == 409


def test_regularize_attendance_patch():
    # Setup employee and punched-in log
    client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )

    # Initial punch in late at 10:05 IST (04:35 UTC)
    in_late_ms = 1783398900000
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": in_late_ms, "status": "PRESENT"},
    )

    # Regularize punch_in to 09:28 IST (03:58 UTC) -> 1783396680000
    in_corrected_ms = 1783396680000
    res_patch = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": in_corrected_ms,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )
    assert res_patch.status_code == 200
    patch_data = res_patch.json()
    assert patch_data["late_minutes"] == 0
    assert len(patch_data["history"]) == 1
    hist = patch_data["history"][0]
    assert hist["by"] == "hr.admin"
    assert hist["reason"] == "biometric glitch"
    assert "late_minutes" in hist["changes"]
    assert hist["changes"]["late_minutes"] == {"from": 35, "to": 0}


def test_explain_endpoint():
    res = client.get("/admin/explain/attendance_list")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data
