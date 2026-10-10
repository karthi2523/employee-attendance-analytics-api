"""
Employee Attendance & Analytics API - Production Reference Implementation
FastAPI, PyMongo, MongoDB Aggregation Pipelines
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import re
from typing import Any, Dict, List, Optional, Union

from bson import json_util

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, errors

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all database indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "message": "Welcome to Employee Attendance & Analytics API",
        "docs": "/docs",
        "redoc": "/redoc",
    }


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Union[float, Decimal], decimals: int = 2) -> float:
    """Perform half-up rounding using Python Decimal."""
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert tz-aware or naive UTC datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a truncated whole-second UTC datetime (R1)."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.replace(microsecond=0)


def parse_time_str(time_str: str) -> time:
    """Parse HH:MM string to time object."""
    h, m = map(int, time_str.split(":"))
    return time(hour=h, minute=m)


def get_attendance_date(punch_in_dt: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """
    Derive IST attendance date (R1).
    For overnight shifts (shift_end <= shift_start), if punch-in IST time is strictly
    earlier than shift_end time, the record belongs to the previous calendar day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    shift_start_t = parse_time_str(shift_start_str)
    shift_end_t = parse_time_str(shift_end_str)

    if shift_end_t <= shift_start_t:
        # Overnight shift
        if punch_in_ist.time() < shift_end_t:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()

    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start_str: str, date_str: str) -> int:
    """
    Compute late minutes (R2).
    Grace period is 10 minutes (strictly > 10 min late).
    Calculated against scheduled shift start on attendance date in IST.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    att_date = date.fromisoformat(date_str)
    shift_start_t = parse_time_str(shift_start_str)
    shift_start_dt = datetime.combine(att_date, shift_start_t, tzinfo=IST)

    elapsed_seconds = int((punch_in_ist - shift_start_dt).total_seconds())
    if elapsed_seconds > 600:  # strictly more than 10 minutes (600 seconds)
        return elapsed_seconds // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    Compute work hours rounded to 2 decimal places half-up (R4).
    """
    sec = int((punch_out_dt - punch_in_dt).total_seconds())
    raw_hours = Decimal(sec) / Decimal(3600)
    return round_half_up(raw_hours, 2)


def compute_overtime(
    punch_out_dt: datetime, shift_start_str: str, shift_end_str: str, date_str: str
) -> int:
    """
    Compute overtime minutes (R3).
    Whole minutes between shift_end and punch_out, counted only when >= 30.
    Handles overnight shifts.
    """
    punch_out_ist = punch_out_dt.astimezone(IST)
    att_date = date.fromisoformat(date_str)
    shift_start_t = parse_time_str(shift_start_str)
    shift_end_t = parse_time_str(shift_end_str)

    if shift_end_t <= shift_start_t:
        # Overnight shift: shift end falls on the next calendar day
        shift_end_dt = datetime.combine(att_date + timedelta(days=1), shift_end_t, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(att_date, shift_end_t, tzinfo=IST)

    ot_seconds = int((punch_out_ist - shift_end_dt).total_seconds())
    ot_minutes = ot_seconds // 60
    if ot_minutes >= 30:
        return ot_minutes
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    Determine if record is half day (R5).
    half_day = true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document to OpenAPI AttendanceRecord response schema."""
    res = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }

    history_list = doc.get("history", []) or []
    formatted_history = []
    for h in history_list:
        changes = {}
        raw_changes = h.get("changes", {})
        for field, change in raw_changes.items():
            fr = change.get("from")
            to = change.get("to")
            if field in ("punch_in", "punch_out"):
                fr = dt_to_epoch_ms(fr) if isinstance(fr, datetime) else fr
                to = dt_to_epoch_ms(to) if isinstance(to, datetime) else to
            changes[field] = {"from": fr, "to": to}

        at_val = h.get("at")
        at_ms = dt_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val

        formatted_history.append(
            {
                "at": at_ms,
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": changes,
            }
        )

    res["history"] = formatted_history
    return res


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document to OpenAPI Employee response schema."""
    created_at = doc.get("created_at")
    created_at_ms = dt_to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


# --------------------------------------------------------------------------- #
# Pydantic Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise HTTPException(422, "shift_start must differ from shift_end")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {str(e)}")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee with unique emp_code constraint."""
    body.validate_shifts()
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except errors.DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record punch-in with atomic date-uniqueness enforcement."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{body.emp_code}' not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_at_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_in_dt = epoch_ms_to_dt(punched_at_ms)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except errors.DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Close employee's open attendance record atomically."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{body.emp_code}' not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_at_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_out_dt = epoch_ms_to_dt(punched_at_ms)

    # Locate open record whose punch_in is <= punch_out_dt
    cursor = db.attendance_logs.find(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}}
    ).sort("punch_in", DESCENDING).limit(1)

    matching_logs = list(cursor)
    if not matching_logs:
        raise HTTPException(404, "No open punch-in found for employee")

    log = matching_logs[0]
    if log.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    punch_in_dt = log["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or > 24 hours after it
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out must be strictly after punch_in")

    duration_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if duration_seconds > 86400:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    att_date = log["date"]
    work_h = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_mins = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], att_date)
    is_half_day = compute_half_day(work_h)

    # Atomic update to avoid race conditions
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": log["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_h,
                "overtime_minutes": ot_mins,
                "half_day": is_half_day,
            }
        },
        return_document=True,
    )

    if not res:
        raise HTTPException(409, "Record is already punched out")

    return format_attendance_doc(res)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date"),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's attendance record with audit trail."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not log:
        raise HTTPException(404, f"Attendance record not found for employee '{emp_code}' on '{date_str}'")

    old_status = log["status"]
    new_status = body.status if body.status is not None else old_status

    old_punch_in = log.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)

    old_punch_out = log.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    # Validate status change rules
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Supplying punch_in or punch_out with ABSENT or LEAVE status is prohibited")
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(422, "Presence status requires a valid punch_in")

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = old_punch_out

    # Validate punch_in stays on record's attendance date
    if new_punch_in is not None:
        derived_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_date != date_str:
            raise HTTPException(422, f"punch_in must stay on the attendance date {date_str}")

    # Validate punch_out constraints
    if new_punch_out is not None and new_punch_in is not None:
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 86400:
            raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived metrics
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    else:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date_str)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date_str)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

    # Compute actual changes
    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != log.get("work_hours"):
        changes["work_hours"] = {"from": log.get("work_hours"), "to": new_work_hours}
    if new_late != log.get("late_minutes", 0):
        changes["late_minutes"] = {"from": log.get("late_minutes", 0), "to": new_late}
    if new_overtime != log.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": log.get("overtime_minutes", 0), "to": new_overtime}
    if new_half_day != log.get("half_day", False):
        changes["half_day"] = {"from": log.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(422, "No fields were modified by this regularization request")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    updated_log = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    return format_attendance_doc(updated_log)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (A - D)
# --------------------------------------------------------------------------- #

def get_month_date_range(month_str: str):
    """Return start_date string and end_date string for YYYY-MM."""
    year, month = map(int, month_str.split("-"))
    start_d = date(year, month, 1)
    if month == 12:
        end_d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_d = date(year, month + 1, 1) - timedelta(days=1)
    return start_d.isoformat(), end_d.isoformat()


def compute_working_days(joined_on_str: str, start_date_str: str, end_date_str: str) -> int:
    """Calculate Mon-Fri working days between start and end date starting from joined_on (R7)."""
    joined_d = date.fromisoformat(joined_on_str)
    period_start = max(date.fromisoformat(start_date_str), joined_d)
    period_end = date.fromisoformat(end_date_str)

    if period_start > period_end:
        return 0

    working_days = 0
    curr = period_start
    while curr <= period_end:
        if curr.weekday() < 5:  # Mon-Fri
            working_days += 1
        curr += timedelta(days=1)
    return working_days


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly_analytics(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, f"Employee '{emp_code}' not found")

    start_date, end_date = get_month_date_range(month)
    w_days = compute_working_days(emp["joined_on"], start_date, end_date)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
        {
            "$project": {
                "status": 1,
                "date": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Sun=1, Mon=2..Fri=6, Sat=7
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = round_half_up(stats.get("present_days", 0.0), 2)
        leave_days = stats.get("leave_days", 0)
        late_count = stats.get("late_count", 0)
        total_late = stats.get("total_late_minutes", 0)
        total_ot = stats.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if w_days > 0:
        att_pct = round_half_up((present_days / w_days) * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": w_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month across all employees & attendance logs."""
    start_date, end_date = get_month_date_range(month)

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "has_log": {"$cond": [{"$ifNull": ["$logs._id", False]}, True, False]},
                "status": "$logs.status",
                "work_hours": "$logs.work_hours",
                "late_minutes": "$logs.late_minutes",
                "half_day": "$logs.half_day",
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        headcount = len(r.get("employees_set", []))
        if headcount == 0:
            continue

        wh_count = r.get("work_hours_count", 0)
        if wh_count > 0:
            avg_wh = round_half_up(r.get("total_work_hours", 0.0) / wh_count, 2)
        else:
            avg_wh = None

        items.append(
            {
                "department": r["_id"],
                "headcount": headcount,
                "present_days": round_half_up(r.get("present_days", 0.0), 2),
                "avg_work_hours": avg_wh,
                "late_count": r.get("late_count", 0),
                "total_late_minutes": r.get("total_late_minutes", 0),
                "leave_count": r.get("leave_count", 0),
                "on_duty_count": r.get("on_duty_count", 0),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard with standard competition ranking."""
    start_date, end_date = get_month_date_range(month)

    emp_match: Dict[str, Any] = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": {"date": {"$gte": start_date, "$lte": end_date}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ]
    )

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard Competition Ranking (1, 2, 2, 4) in Python
    ranked_items = []
    current_rank = 1
    for i, row in enumerate(rows):
        if i > 0:
            prev_minutes = rows[i - 1]["total_late_minutes"]
            if row["total_late_minutes"] < prev_minutes:
                current_rank = i + 1

        if current_rank > limit:
            break

        ranked_items.append(
            {
                "rank": current_rank,
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": row["total_late_minutes"],
                "late_count": row["late_count"],
            }
        )

    return {"month": month, "items": ranked_items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend_analytics(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Daily attendance trend for a department using MongoDB $densify pipeline."""
    if from_date > to_date:
        raise HTTPException(422, "from date cannot be greater than to date")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if (d_to - d_from).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    # Verify department exists
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(404, f"Department '{department}' not found")

    # Department daily headcount lookup map
    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1}))

    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {"$project": {"date_str": "$_id", "present_count": 1, "late_count": 1, "_id": 0}},
        {
            "$project": {
                "date_obj": {"$dateFromString": {"dateString": "$date_str"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(d_from, time.min, tzinfo=timezone.utc),
                        datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=timezone.utc),
                    ],
                },
            }
        },
        {"$sort": {"date_obj": ASCENDING}},
    ]

    try:
        raw_trend = list(db.employees.aggregate(pipeline))
    except Exception:
        # Fallback if $densify is restricted in environment
        raw_trend = []

    # Map raw_trend by YYYY-MM-DD
    trend_map = {}
    for r in raw_trend:
        dt_val = r.get("date_obj")
        if isinstance(dt_val, datetime):
            d_str = dt_val.strftime("%Y-%m-%d")
        else:
            d_str = str(dt_val)[:10]
        trend_map[d_str] = r

    # Build exact daily series for range
    items = []
    rates_history = []  # non-null attendance_rate values in range

    curr = d_from
    while curr <= d_to:
        curr_str = curr.isoformat()
        is_working = curr.weekday() < 5

        # Calculate headcount for this date
        hc = sum(1 for e in dept_employees if e["joined_on"] <= curr_str)

        log_data = trend_map.get(curr_str, {})
        pres_cnt = round_half_up(log_data.get("present_count", 0.0), 2)
        late_cnt = log_data.get("late_count", 0)

        if is_working and hc > 0:
            rate = round_half_up(pres_cnt / hc, 4)
        else:
            rate = None

        rates_history.append(rate)

        # 7-day moving average over non-null rates in window up to 6 preceding days
        window = rates_history[-7:]
        valid_rates = [r for r in window if r is not None]
        if valid_rates:
            m_avg = round_half_up(sum(valid_rates) / len(valid_rates), 4)
        else:
            m_avg = None

        items.append(
            {
                "date": curr_str,
                "is_working_day": is_working,
                "headcount": hc,
                "present_count": pres_cnt,
                "late_count": late_cnt,
                "attendance_rate": rate,
                "moving_avg_7d": m_avg,
            }
        )
        curr += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., description="Target endpoint name"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Returns raw MongoDB explain output (executionStats) for endpoint's primary query."""
    allowed_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in allowed_endpoints:
        raise HTTPException(422, f"Invalid endpoint name. Must be one of {allowed_endpoints}")

    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        expl = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(expl))}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month parameters are required for employee_monthly explain")
        start_date, end_date = get_month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
            {"$group": {"_id": None, "total_late": {"$sum": "$late_minutes"}}},
        ]
        expl = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(expl))}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month parameter is required for department_summary explain")
        start_date, end_date = get_month_date_range(month)
        emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs",
                }
            },
        ]
        expl = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(expl))}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month parameter is required for late_leaderboard explain")
        start_date, end_date = get_month_date_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": start_date, "$lte": end_date}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        expl = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(expl))}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to parameters are required for department_trend explain")
        pipeline = [
            {"$match": {"department": department}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs",
                }
            },
        ]
        expl = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(expl))}
