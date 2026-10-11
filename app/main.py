"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables already set in the real environment

log = logging.getLogger("attendance")

# tz_aware=True: stored instants come back as UTC-aware datetimes, so they can be compared with
# and converted from epoch ms without guessing a zone. The short server-selection timeout lets
# /health answer 503 quickly instead of hanging for PyMongo's 30 s default.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ONE_SECOND = timedelta(seconds=1)
MAX_SHIFT_LENGTH = timedelta(hours=24)
HALF_DAY_BELOW = 4.5  # R5, compared with the rounded work_hours
NOT_PRESENT = ("ABSENT", "LEAVE")
WRITE_ATTEMPTS = 10  # optimistic-concurrency retries before a conditional write gives up with 409


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent. The unique indexes are what make concurrent creates/punch-ins race-safe."""
    global _indexes_ready
    if _indexes_ready:
        return
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_date_unique"
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # MongoDB not reachable yet: /health reports 503 and retries
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# Every instant inside the app is a UTC-aware datetime truncated to whole seconds (R1).
# Epoch ms only exist at the HTTP boundary (from_millis / to_millis).
def from_millis(ms: int) -> datetime:
    return EPOCH + timedelta(seconds=ms // 1000)


def to_millis(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else (dt - EPOCH) // timedelta(milliseconds=1)


def truncate(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def bson_now() -> datetime:
    """Server clock as stored by MongoDB (BSON dates keep milliseconds), so the response matches what is stored."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def round_half_up(value, places: int) -> float:
    """value must be an int, str or Decimal (a float would carry its binary error into the rounding)."""
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded "HH:MM" strings compare correctly


def shift_instant(date_str: str, hhmm: str, days: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(date.fromisoformat(date_str), time(h, m), tzinfo=IST) + timedelta(days=days)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in; an overnight-shift punch-in before shift_end belongs to the previous day."""
    local = punch_in.astimezone(IST)
    h, m = map(int, shift_end.split(":"))
    if is_overnight(shift_start, shift_end) and local.time() < time(h, m):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = (truncate(punch_in) - shift_instant(date_str, shift_start)) // ONE_SECOND
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: whole seconds / 3600, 2 decimals half-up."""
    seconds = (truncate(punch_out) - truncate(punch_in)) // ONE_SECOND
    return round_half_up(Decimal(seconds) / 3600, 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes after shift end (next day for an overnight shift), counted only when >= 30."""
    end = shift_instant(date_str, shift_end, days=1 if is_overnight(shift_start, shift_end) else 0)
    minutes = ((truncate(punch_out) - end) // ONE_SECOND) // 60
    return minutes if minutes >= 30 else 0


def punch_out_fields(punch_in: datetime, punch_out: datetime, emp: dict, date_str: str) -> dict:
    """R3-R5: what punch-out (and a correction that leaves a punch_out) stores."""
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str),
        "half_day": work_hours < HALF_DAY_BELOW,
    }


def derived_fields(punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, date_str: str) -> dict:
    """R2-R5 from the final times, with DATA_MODEL's values for an open record and for ABSENT / LEAVE (no punch_in)."""
    fields = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is not None:
        fields["late_minutes"] = compute_late_minutes(punch_in, emp["shift_start"], date_str)
        if punch_out is not None:
            fields.update(punch_out_fields(punch_in, punch_out, emp, date_str))
    return fields


def validation_error(loc: tuple, msg: str, value) -> RequestValidationError:
    """A 422 with FastAPI's default body ({"detail": [...]}), for rules a pydantic model cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def check_punch_out_window(punch_in: datetime, punch_out: datetime, loc: tuple) -> None:
    if punch_out <= punch_in:
        raise validation_error(loc, "punch_out must be after punch_in", to_millis(punch_out))
    if punch_out - punch_in > MAX_SHIFT_LENGTH:
        raise validation_error(loc, "punch_out must be within 24 hours of punch_in", to_millis(punch_out))


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_millis(doc.get("created_at")),
    }


def history_entry_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            change = {"from": to_millis(change.get("from")), "to": to_millis(change.get("to"))}
        changes[field] = change
    return {"at": to_millis(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


CORRECTABLE_FIELDS = ("status", "punch_in", "punch_out")
TRACKED_FIELDS = CORRECTABLE_FIELDS + ("work_hours", "late_minutes", "overtime_minutes", "half_day")


def stored_values(doc: dict) -> dict:
    """The record's fields as the API sees them. Legacy documents may omit late / overtime / half_day
    (DATA_MODEL: 0 / 0 / false); work_hours has no documented default and stays null."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": doc.get("half_day") or False,
    }


def record_out(doc: dict) -> dict:
    """AttendanceRecord. A legacy document without history has had no corrections (DATA_MODEL: [])."""
    values = stored_values(doc)
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        **values,
        "punch_in": to_millis(values["punch_in"]),
        "punch_out": to_millis(values["punch_out"]),
        "history": [history_entry_out(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def _real_date(value: str) -> str:
    date.fromisoformat(value)  # ValueError (e.g. 2026-02-30) becomes a 422
    return value


# [0-9] rather than \d: pydantic's regex engine treats \d as any Unicode digit.
DateStr = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_real_date)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# strict: rejects floats (even 1.0), numeric strings and booleans; bounds reject seconds-looking values.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_has_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None  # same as punch-in: omitted (or null) means now


class RegularizeIn(BaseModel):
    """Omitted means unchanged. The schema is not nullable, so an explicit null fails validation (422): the
    None defaults are not validated. Derived fields sent by a client are ignored (pydantic drops extra keys)."""
    status: Status = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "MongoDB is not available"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    ensure_indexes()
    doc = body.model_dump()
    doc["created_at"] = bson_now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    ensure_indexes()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_millis(body.punched_at) if body.punched_at is not None else truncate(datetime.now(timezone.utc))
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def attendance_list_query(emp_code, date_from, date_to, status) -> dict:
    q = {}
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
    return q


ATTENDANCE_LIST_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[DateStr], Query()] = None,
    date_to: Annotated[Optional[DateStr], Query()] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if date_from and date_to and date_from > date_to:
        raise validation_error(("query", "date_from"), "date_from must be on or before date_to", date_from)
    q = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_LIST_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Punch-out and corrections
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_millis(body.punched_at) if body.punched_at is not None else truncate(datetime.now(timezone.utc))
    for _ in range(WRITE_ATTEMPTS):
        # The most recent punch-in at or before the punch-out, whatever its date: an overnight shift is closed the
        # next calendar day. $lte on a date never matches a null / missing punch_in, so ABSENT / LEAVE are skipped.
        rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
        )
        if rec is None:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:  # that record is closed; never fall back to an older open one
            raise HTTPException(409, "already punched out")
        check_punch_out_window(rec["punch_in"], ts, ("body", "punched_at"))
        # Conditional write: of simultaneous punch-outs only one can still match punch_out == null (the others
        # re-read, see it closed and get 409), and none can store values computed from a punch_in that a concurrent
        # correction has since changed (that one re-reads and recomputes). late_minutes and history are not
        # punch-out's to change.
        updated = db.attendance_logs.find_one_and_update(
            {"_id": rec["_id"], "punch_in": rec["punch_in"], "punch_out": None},
            {"$set": {"punch_out": ts, **punch_out_fields(rec["punch_in"], ts, emp, rec["date"])}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "the record is being changed concurrently; retry")


def regularized_values(rec: dict, body: RegularizeIn, emp: dict) -> dict:
    """The record's final status, punch times and derived fields (R2-R5) after applying the request, or a 422."""
    old = stored_values(rec)
    status = body.status if body.status is not None else old["status"]
    if status in NOT_PRESENT:
        for field in ("punch_in", "punch_out"):
            if getattr(body, field) is not None:
                raise validation_error(("body", field), f"{status} clears both punch times; {field} cannot be set",
                                       getattr(body, field))
        punch_in = punch_out = None
    else:
        punch_in = from_millis(body.punch_in) if body.punch_in is not None else old["punch_in"]
        if punch_in is None:
            raise validation_error(("body", "punch_in"), f"{status} requires a punch_in", None)
        if body.punch_in is not None and attendance_date(punch_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise validation_error(("body", "punch_in"), f"punch_in must fall on the attendance date {rec['date']}",
                                   body.punch_in)
        punch_out = from_millis(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if punch_out is not None:  # checked against the final punch_in, so moving only punch_in is covered too
            loc = ("body", "punch_out" if body.punch_out is not None else "punch_in")
            check_punch_out_window(punch_in, punch_out, loc)
    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out}
    # "Changes nothing" is judged on what the client can set: a request equal to the stored status and times is a
    # 422 even if the stored derived values are stale (they are authoritative and are not rewritten as a side effect).
    if all(new[f] == old[f] for f in CORRECTABLE_FIELDS):
        raise validation_error(("body",), "the request changes nothing", None)
    return {**new, **derived_fields(punch_in, punch_out, emp, rec["date"])}


def unchanged_since_read(rec: dict) -> dict:
    """Matches the record only while everything the correction was computed from still holds the value that was read.
    Every successful correction or punch-out changes status, a punch time or the history length."""
    history = rec.get("history")
    return {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),     # None also matches a missing field
        "punch_out": rec.get("punch_out"),
        "history": {"$size": len(history)} if isinstance(history, list) else None,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: Annotated[DateStr, Path()], body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    # Optimistic concurrency: compute from the record as read, write only if it is unchanged. A concurrent writer
    # makes the write miss; then re-read and re-apply the request on top of the winner, so each success appends
    # exactly one entry whose "from" values are the real previous state. Only sustained contention ends in 409.
    for _ in range(WRITE_ATTEMPTS):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for this date")
        old = stored_values(rec)
        new = regularized_values(rec, body, emp)
        entry = {
            "at": bson_now(),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]},
        }
        if isinstance(rec.get("history"), list):
            update = {"$set": new, "$push": {"history": entry}}
        else:  # legacy record without history (DATA_MODEL: missing means empty)
            update = {"$set": {**new, "history": [entry]}}
        updated = db.attendance_logs.find_one_and_update(
            unchanged_since_read(rec), update, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "the record is being changed concurrently; retry")


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
