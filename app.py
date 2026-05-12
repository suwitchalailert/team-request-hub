from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import uuid
import os
import json

app = Flask(__name__)

URGENCY_ORDER = {"สูง": 0, "ปานกลาง": 1, "ต่ำ": 2}
STATUS_OPTIONS = ["รอดำเนินการ", "กำลังทำ", "เสร็จแล้ว", "ยกเลิก"]
WORK_TYPES = ["IT", "OFFICE", "OTHER", "PERSON"]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

if USE_SUPABASE:
    from supabase import create_client
    _sb = create_client(SUPABASE_URL, SUPABASE_KEY)

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
TABLE = "requests"


def _parse_ts(ts):
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None)
    ts_clean = str(ts)[:19].replace("T", " ")
    try:
        return datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now()


def _normalize(r):
    r["created_at"] = _parse_ts(r.get("created_at", ""))
    r["amount"] = r.get("amount") if r.get("amount") not in (None, "") else ""
    r.setdefault("work_type", "OTHER")
    if not r.get("created_at_str"):
        r["created_at_str"] = r["created_at"].strftime("%d/%m/%Y %H:%M")
    return r


def load_all():
    if USE_SUPABASE:
        res = _sb.table(TABLE).select("*").execute()
        return [_normalize(r) for r in (res.data or [])]
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)
    return [_normalize(r) for r in records]


def _save_local(records):
    rows = []
    for r in records:
        row = {**r, "created_at": r["created_at"].isoformat() if isinstance(r["created_at"], datetime) else r["created_at"]}
        rows.append(row)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def db_insert(rec):
    if USE_SUPABASE:
        row = {k: v for k, v in rec.items()}
        row["created_at"] = rec["created_at"].isoformat()
        row["amount"] = rec["amount"] if rec["amount"] != "" else None
        _sb.table(TABLE).insert(row).execute()
    else:
        records = load_all()
        records.append(rec)
        _save_local(records)


def db_update(req_id, updates):
    if USE_SUPABASE:
        sb_updates = {**updates}
        if "amount" in sb_updates and sb_updates["amount"] == "":
            sb_updates["amount"] = None
        _sb.table(TABLE).update(sb_updates).eq("id", req_id).execute()
    else:
        records = load_all()
        for r in records:
            if r["id"] == req_id:
                r.update(updates)
                break
        _save_local(records)


def find_record(req_id):
    if USE_SUPABASE:
        res = _sb.table(TABLE).select("*").eq("id", req_id).execute()
        rows = res.data or []
        return _normalize(rows[0]) if rows else None
    records = load_all()
    return next((r for r in records if r["id"] == req_id), None)


def get_sorted(records):
    return sorted(records, key=lambda r: (URGENCY_ORDER.get(r["urgency"], 99), r["created_at"]))


def parse_amount(raw):
    try:
        val = float(str(raw).replace(",", "").strip())
        return val if val >= 0 else ""
    except (ValueError, AttributeError):
        return ""


def fmt_amount(val):
    if val == "" or val is None:
        return ""
    try:
        return f"{float(val):,.2f}"
    except (ValueError, TypeError):
        return ""


app.jinja_env.filters["fmt_amount"] = fmt_amount


def build_counts(all_requests):
    return {
        "total": len(all_requests),
        "pending": sum(1 for r in all_requests if r["status"] == "รอดำเนินการ"),
        "in_progress": sum(1 for r in all_requests if r["status"] == "กำลังทำ"),
        "done": sum(1 for r in all_requests if r["status"] == "เสร็จแล้ว"),
        "cancelled": sum(1 for r in all_requests if r["status"] == "ยกเลิก"),
    }


def build_work_type_stats(all_requests):
    stats = {}
    for wt in WORK_TYPES:
        items = [r for r in all_requests if r.get("work_type") == wt]
        active = [r for r in items if r["status"] != "ยกเลิก"]
        total_amount = sum(float(r["amount"]) for r in active if r.get("amount") not in ("", None))
        stats[wt] = {
            "total": len(items),
            "pending": sum(1 for r in items if r["status"] == "รอดำเนินการ"),
            "in_progress": sum(1 for r in items if r["status"] == "กำลังทำ"),
            "done": sum(1 for r in items if r["status"] == "เสร็จแล้ว"),
            "cancelled": sum(1 for r in items if r["status"] == "ยกเลิก"),
            "total_amount": total_amount,
        }
    return stats


@app.route("/")
def dashboard():
    all_requests = get_sorted(load_all())
    counts = build_counts(all_requests)
    work_type_stats = build_work_type_stats(all_requests)
    return render_template("dashboard.html", requests=all_requests, counts=counts,
                           status_options=STATUS_OPTIONS, work_types=WORK_TYPES,
                           work_type_stats=work_type_stats)


@app.route("/submit", methods=["GET", "POST"])
def submit():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        urgency = request.form.get("urgency", "ปานกลาง")
        work_type = request.form.get("work_type", "OTHER")
        amount = parse_amount(request.form.get("amount", ""))

        if not name or not description:
            return render_template("submit.html", error="กรุณากรอกข้อมูลให้ครบถ้วน",
                                   work_types=WORK_TYPES)

        now = datetime.now()
        new_request = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "description": description,
            "urgency": urgency,
            "work_type": work_type if work_type in WORK_TYPES else "OTHER",
            "amount": amount,
            "status": "รอดำเนินการ",
            "created_at": now,
            "created_at_str": now.strftime("%d/%m/%Y %H:%M"),
        }
        db_insert(new_request)
        return redirect(url_for("submit_success"))

    return render_template("submit.html", work_types=WORK_TYPES)


@app.route("/submit/success")
def submit_success():
    return render_template("success.html")


@app.route("/edit/<req_id>", methods=["GET", "POST"])
def edit_request(req_id):
    req = find_record(req_id)
    if not req or req["status"] in ("ยกเลิก", "เสร็จแล้ว"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        old_amount = req["amount"]
        new_amount = parse_amount(request.form.get("amount", ""))
        work_type = request.form.get("work_type", req["work_type"])

        updates = {
            "name": request.form.get("name", req["name"]).strip() or req["name"],
            "description": request.form.get("description", req["description"]).strip() or req["description"],
            "urgency": request.form.get("urgency", req["urgency"]),
            "work_type": work_type if work_type in WORK_TYPES else req["work_type"],
            "amount": new_amount,
        }
        if req["status"] == "เสร็จแล้ว" or new_amount != old_amount:
            updates["status"] = "รอดำเนินการ"

        db_update(req_id, updates)
        return redirect(url_for("dashboard"))

    return render_template("edit.html", req=req, work_types=WORK_TYPES)


@app.route("/update-status", methods=["POST"])
def update_status():
    req_id = request.form.get("id")
    new_status = request.form.get("status")
    req = find_record(req_id)
    if req and req["status"] != "ยกเลิก" and new_status in STATUS_OPTIONS:
        db_update(req_id, {"status": new_status})
    return redirect(url_for("dashboard"))


@app.route("/dashboard/person")
def dashboard_person():
    all_requests = get_sorted(load_all())
    person_map = {}
    for r in all_requests:
        person_map.setdefault(r["name"], []).append(r)

    persons = []
    for name, items in person_map.items():
        active = [r for r in items if r["status"] != "ยกเลิก"]
        total_amount = sum(float(r["amount"]) for r in active if r.get("amount") not in ("", None))
        persons.append({
            "name": name,
            "requests": items,
            "total": len(items),
            "pending": sum(1 for r in items if r["status"] == "รอดำเนินการ"),
            "in_progress": sum(1 for r in items if r["status"] == "กำลังทำ"),
            "done": sum(1 for r in items if r["status"] == "เสร็จแล้ว"),
            "cancelled": sum(1 for r in items if r["status"] == "ยกเลิก"),
            "total_amount": total_amount,
        })

    persons.sort(key=lambda p: p["total"], reverse=True)
    return render_template("person_dashboard.html", persons=persons,
                           status_options=STATUS_OPTIONS, work_types=WORK_TYPES)


@app.route("/dashboard/expense")
def dashboard_expense():
    all_requests = load_all()
    active = [r for r in all_requests if r["status"] != "ยกเลิก" and r.get("amount") not in ("", None)]

    by_day, by_month, by_year = {}, {}, {}
    for r in active:
        dt = r["created_at"]
        amount = float(r["amount"])
        wt = r.get("work_type", "OTHER")
        for bucket, key in [(by_day, dt.strftime("%d/%m/%Y")),
                            (by_month, dt.strftime("%m/%Y")),
                            (by_year, dt.strftime("%Y"))]:
            if key not in bucket:
                bucket[key] = {"total": 0, "count": 0, "by_type": {w: 0 for w in WORK_TYPES}}
            bucket[key]["total"] += amount
            bucket[key]["count"] += 1
            bucket[key]["by_type"][wt] += amount

    return render_template("expense_dashboard.html",
                           by_day=sorted(by_day.items()),
                           by_month=sorted(by_month.items()),
                           by_year=sorted(by_year.items()),
                           work_types=WORK_TYPES)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
