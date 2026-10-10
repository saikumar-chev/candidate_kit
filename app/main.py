"""Employee Attendance & Analytics API."""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MAX_MS = 4_102_444_800_000
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENT | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def date_value(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be YYYY-MM-DD") from exc
    return value


def month_value(value: str) -> str:
    if len(value) != 7 or value[4] != "-":
        raise ValueError("must be YYYY-MM")
    date_value(value + "-01")
    return value


def request_date(value: str) -> str:
    try:
        return date_value(value)
    except ValueError as exc:
        raise HTTPException(422, "invalid date") from exc


def request_month(value: str) -> str:
    try:
        return month_value(value)
    except ValueError as exc:
        raise HTTPException(422, "invalid month") from exc


def time_value(value: str) -> str:
    if len(value) != 5 or value[2] != ":":
        raise ValueError("must be HH:MM")
    hour, minute = value.split(":")
    if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ValueError("must be HH:MM")
    return value


def db_time(milliseconds: int) -> datetime:
    if type(milliseconds) is not int or not 100_000_000_000 <= milliseconds <= MAX_MS:
        raise ValueError("must be epoch milliseconds")
    return datetime.fromtimestamp(milliseconds // 1000, UTC).replace(tzinfo=None)


def server_time() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def epoch(value: Optional[datetime]) -> Optional[int]:
    return None if value is None else int(utc(value).timestamp() * 1000)


def rounded(value: float, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parts(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def attendance_day(punch_in: datetime, employee: dict) -> str:
    local = utc(punch_in).astimezone(IST)
    start, end = parts(employee["shift_start"]), parts(employee["shift_end"])
    if end <= start and (local.hour, local.minute) < end:
        local -= timedelta(days=1)
    return local.date().isoformat()


def shift_time(day: str, value: str, next_day: bool = False) -> datetime:
    result = datetime.fromisoformat(day).replace(tzinfo=IST)
    if next_day:
        result += timedelta(days=1)
    hour, minute = parts(value)
    return result.replace(hour=hour, minute=minute, second=0, microsecond=0)


def derived(punch_in: Optional[datetime], punch_out: Optional[datetime], employee: dict, day: str) -> dict:
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    start, end = parts(employee["shift_start"]), parts(employee["shift_end"])
    late_elapsed = int((utc(punch_in) - shift_time(day, employee["shift_start"]).astimezone(UTC)).total_seconds() // 60)
    late = late_elapsed if late_elapsed > 10 else 0
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = rounded((utc(punch_out) - utc(punch_in)).total_seconds() / 3600)
    end_time = shift_time(day, employee["shift_end"], end <= start)
    overtime = int((utc(punch_out) - end_time.astimezone(UTC)).total_seconds() // 60)
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime if overtime >= 30 else 0, "half_day": hours < 4.50}


def employee_json(doc: dict) -> dict:
    result = {field: doc[field] for field in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    result["created_at"] = epoch(doc["created_at"])
    return result


def attendance_json(doc: dict) -> dict:
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            convert = field in {"punch_in", "punch_out"}
            changes[field] = {"from": epoch(change["from"]) if convert else change["from"], "to": epoch(change["to"]) if convert else change["to"]}
        history.append({"at": epoch(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes})
    return {"emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"], "punch_in": epoch(doc.get("punch_in")), "punch_out": epoch(doc.get("punch_out")), "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0), "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False), "history": history}


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    _joined = field_validator("joined_on")(date_value)
    _start = field_validator("shift_start")(time_value)
    _end = field_validator("shift_end")(time_value)

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100_000_000_000, le=MAX_MS)
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def presence_status(cls, value: str) -> str:
        if value not in PRESENT:
            raise ValueError("status must be a presence status")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100_000_000_000, le=MAX_MS)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(None, ge=100_000_000_000, le=MAX_MS)
    punch_out: Optional[StrictInt] = Field(None, ge=100_000_000_000, le=MAX_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value


def indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employees_emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="employees_department_joined")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="employees_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_emp_date_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING), ("punch_out", ASCENDING)], name="attendance_punch_lookup")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_analytics")


@app.on_event("startup")
def startup():
    indexes()


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(503, "MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(); doc["created_at"] = server_time()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return employee_json(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department else {}
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_json(doc) for doc in cursor], "total": db.employees.count_documents(query), "page": page, "page_size": page_size}


def employee_or_404(code: str) -> dict:
    employee = db.employees.find_one({"emp_code": code})
    if not employee:
        raise HTTPException(404, "employee not found")
    return employee


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    punched = db_time(body.punched_at) if body.punched_at is not None else server_time()
    day = attendance_day(punched, employee)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": punched, "punch_out": None, **derived(punched, None, employee, day), "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    punched = db_time(body.punched_at) if body.punched_at is not None else server_time()
    current = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched}}, sort=[("punch_in", DESCENDING)])
    if not current:
        raise HTTPException(404, "no punch-in found")
    if current.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    if punched <= current["punch_in"] or punched - current["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    values = derived(current["punch_in"], punched, employee, current["date"])
    updated = db.attendance_logs.find_one_and_update({"_id": current["_id"], "punch_out": None}, {"$set": {"punch_out": punched, **values}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(409, "record is already punched out")
    return attendance_json(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: request_date(date_from)
    if date_to: request_date(date_to)
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not be after date_to")
    if status and status not in STATUSES: raise HTTPException(422, "invalid status")
    query = {key: value for key, value in (("emp_code", emp_code), ("status", status)) if value}
    if date_from or date_to: query["date"] = {key: value for key, value in (("$gte", date_from), ("$lte", date_to)) if value}
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_json(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    request_date(date); employee = employee_or_404(emp_code)
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not current: raise HTTPException(404, "attendance record not found")
    status = body.status or current["status"]
    if status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None): raise HTTPException(422, "absent and leave records cannot have punch times")
    punch_in = db_time(body.punch_in) if body.punch_in is not None else current.get("punch_in")
    punch_out = db_time(body.punch_out) if body.punch_out is not None else current.get("punch_out")
    if status in {"ABSENT", "LEAVE"}: punch_in = punch_out = None
    elif punch_in is None: raise HTTPException(422, "a presence record requires punch_in")
    if punch_in is not None and attendance_day(punch_in, employee) != date: raise HTTPException(422, "punch_in must belong to the attendance date")
    if punch_out is not None and (punch_out <= punch_in or punch_out - punch_in > timedelta(hours=24)): raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    values = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False} if status in {"ABSENT", "LEAVE"} else derived(punch_in, punch_out, employee, date)
    final = {"status": status, "punch_in": punch_in, "punch_out": punch_out, **values}
    defaults = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    changes = {field: {"from": current.get(field, defaults.get(field)), "to": value} for field, value in final.items() if current.get(field, defaults.get(field)) != value}
    if not changes: raise HTTPException(422, "request does not change the record")
    history = {"at": server_time(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_filter = {"history": current["history"]} if "history" in current else {"history": {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update({"_id": current["_id"], **history_filter}, {"$set": final, "$push": {"history": history}}, return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409, "attendance record changed concurrently")
    return attendance_json(updated)


def bounds(month: str) -> tuple[str, str]:
    request_month(month); first = date.fromisoformat(month + "-01")
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat()


def log_match(month: str, code: Optional[str] = None) -> dict:
    first, last = bounds(month); result = {"date": {"$gte": first, "$lte": last}}
    if code: result["emp_code"] = code
    return result


@app.get("/analytics/employees/{emp_code}/monthly")
def monthly(emp_code: str, month: str):
    employee = employee_or_404(emp_code); first, last = bounds(month)
    start, finish, joined = date.fromisoformat(first), date.fromisoformat(last), date.fromisoformat(employee["joined_on"])
    working = sum((start + timedelta(days=i)).weekday() < 5 and start + timedelta(days=i) >= joined for i in range((finish - start).days + 1))
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
    present_record = {"$and": [{"$in": ["$status", list(PRESENT)]}, weekday]}
    pipeline = [{"$match": log_match(month, emp_code)}, {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [present_record, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]
    row = next(iter(db.attendance_logs.aggregate(pipeline)), {}); present = row.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present, "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": None if working == 0 else rounded(present / working * 100)}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = bounds(month); employee_filter = {"joined_on": {"$lte": last}}
    if department: employee_filter["department"] = department
    weekday_log = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
    present_log = {"$and": [{"$in": ["$logs.status", list(PRESENT)]}, weekday_log]}
    pipeline = [{"$match": employee_filter}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]}]}}}], "as": "logs"}}, {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}}, {"$group": {"_id": "$department", "employee_codes": {"$addToSet": "$emp_code"}, "present_days": {"$sum": {"$cond": [present_log, {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}}, "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", "$$REMOVE"]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}}, {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$employee_codes"}, "present_days": 1, "avg_work_hours": {"$cond": [{"$gt": [{"$size": "$hours"}, 0]}, {"$avg": "$hours"}, None]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item["avg_work_hours"] is not None: item["avg_work_hours"] = rounded(item["avg_work_hours"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = bounds(month)
    pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}}, {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}}, {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}}, {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    request_date(from_); request_date(to); start, finish = date.fromisoformat(from_), date.fromisoformat(to)
    if finish < start: raise HTTPException(422, "to must not be before from")
    if (finish - start).days > 91: raise HTTPException(422, "range must not exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(finish + timedelta(days=1), datetime.min.time())
    pipeline = [
        {"$documents": [{"date_dt": start_dt}, {"date_dt": end_dt}]},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"$expr": {"$eq": ["$employee.department", department]}}},
            {"$project": {"date_dt": {"$dateFromString": {"dateString": "$date"}}, "present_count": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}, "late_count": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}
        ]}},
        {"$densify": {"field": "date_dt", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {"$match": {"date_dt": {"$lt": end_dt}}},
        {"$group": {"_id": "$date_dt", "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}}, "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}}}},
        {"$set": {"date_dt": "$_id", "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}}, "is_working_day": {"$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}}], "as": "joined_employees"}},
        {"$set": {"headcount": {"$size": "$joined_employees"}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date_dt": 1}, "output": {"rate_window": {"$push": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": {"$let": {"vars": {"rates": {"$filter": {"input": "$rate_window", "as": "rate", "cond": {"$ne": ["$$rate", None]}}}}, "in": {"$cond": [{"$gt": [{"$size": "$$rates"}, 0]}, {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$avg": "$$rates"}, 10000]}, 0.5]}}, 10000]}, None]}}}}},
        {"$project": {"_id": 0, "date_dt": 0, "rate_window": 0, "joined_employees": 0}},
        {"$sort": {"date": 1}}
    ]
    return {"department": department, "items": list(db.aggregate(pipeline))}


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed: raise HTTPException(422, "unknown endpoint")
    if endpoint == "attendance_list":
        query = {key: value for key, value in (("emp_code", emp_code), ("status", status)) if value}
        if date_from or date_to: query["date"] = {key: value for key, value in (("$gte", date_from), ("$lte", date_to)) if value}
        raw = db.command("explain", {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw}
    if not month and endpoint != "department_trend": raise HTTPException(422, "month is required")
    if endpoint == "department_trend" and (not department or not from_ or not to): raise HTTPException(422, "department, from and to are required")
    if endpoint == "employee_monthly" and not emp_code: raise HTTPException(422, "emp_code is required")
    if endpoint == "department_summary":
        first, last = bounds(month); query = {"joined_on": {"$lte": last}}
        if department: query["department"] = department
        raw = db.command("explain", {"find": "employees", "filter": query, "sort": {"department": 1}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": raw}
    if endpoint == "department_trend":
        query = {"department": department, "joined_on": {"$lte": to}}
        raw = db.command("explain", {"find": "employees", "filter": query}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": raw}
    first, last = bounds(month)
    query = {"date": {"$gte": first, "$lte": last}}
    if endpoint == "employee_monthly": query["emp_code"] = emp_code
    if endpoint == "late_leaderboard": query["late_minutes"] = {"$gt": 0}
    raw = db.command("explain", {"find": "attendance_logs", "filter": query}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw}
