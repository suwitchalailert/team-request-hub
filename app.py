from flask import Flask, render_template, request, redirect, url_for, Response
from datetime import datetime
import uuid
import os
import json
import csv
import io

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
BUDGETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "budgets.json")
TABLE = "requests"
BUDGETS_TABLE = "budgets"
BUCKET = "attachments"


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
    r.setdefault("attachment_url", None)
    if not r.get("created_at_str"):
        r["created_at_str"] = r["created_at"].strftime("%d/%m/%Y %H:%M")
    return r


def upload_attachment(file_obj):
    if not USE_SUPABASE or not file_obj or not file_obj.filename:
        return None
    ext = file_obj.filename.rsplit(".", 1)[-1].lower() if "." in file_obj.filename else "bin"
    import uuid as _uuid
    path = f"{_uuid.uuid4().hex}.{ext}"
    data = file_obj.read()
    content_type = file_obj.content_type or "application/octet-stream"
    _sb.storage.from_(BUCKET).upload(path, data, {"content-type": content_type})
    return _sb.storage.from_(BUCKET).get_public_url(path)


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
        row.pop("attachment_url", None)
        if rec.get("attachment_url"):
            row["attachment_url"] = rec["attachment_url"]
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


def load_budgets():
    if USE_SUPABASE:
        res = _sb.table(BUDGETS_TABLE).select("*").execute()
        return res.data or []
    if not os.path.exists(BUDGETS_FILE):
        return []
    with open(BUDGETS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_budgets_local(budgets):
    with open(BUDGETS_FILE, "w", encoding="utf-8") as f:
        json.dump(budgets, f, ensure_ascii=False, indent=2)


def db_budget_upsert(b):
    if USE_SUPABASE:
        _sb.table(BUDGETS_TABLE).upsert(b).execute()
    else:
        budgets = [x for x in load_budgets() if x["id"] != b["id"]]
        budgets.append(b)
        _save_budgets_local(budgets)


def db_budget_delete(budget_id):
    if USE_SUPABASE:
        _sb.table(BUDGETS_TABLE).delete().eq("id", budget_id).execute()
    else:
        _save_budgets_local([b for b in load_budgets() if b["id"] != budget_id])


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
        attachment_url = upload_attachment(request.files.get("attachment"))

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
            "attachment_url": attachment_url,
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


@app.route("/budget", methods=["GET", "POST"])
def budget_manage():
    from collections import defaultdict
    all_requests = load_all()
    persons = sorted(set(r["name"] for r in all_requests))
    budgets = load_budgets()

    def period_sort_key(p):
        parts = p.split("/")
        return (parts[1], parts[0].zfill(2)) if len(parts) == 2 else (p, "")

    def build_pivot(blist, ptype):
        periods = sorted(set(b["period"] for b in blist if b["period_type"] == ptype), key=period_sort_key)
        lookup = defaultdict(dict)
        for b in blist:
            if b["period_type"] == ptype:
                lookup[(b["category"], b["name"])][b["period"]] = b
        seen, person_rows, wt_rows = set(), [], []
        for b in sorted(blist, key=lambda x: x["name"]):
            if b["period_type"] != ptype:
                continue
            key = (b["category"], b["name"])
            if key in seen:
                continue
            seen.add(key)
            row = {"name": b["name"], "category": b["category"],
                   "cells": {p: lookup[key].get(p) for p in periods}}
            (person_rows if b["category"] == "person" else wt_rows).append(row)
        return periods, person_rows, wt_rows

    m_periods, m_person, m_wt = build_pivot(budgets, "monthly")
    y_periods, y_person, y_wt = build_pivot(budgets, "yearly")

    return render_template("budget.html", budgets=budgets, persons=persons, work_types=WORK_TYPES,
                           m_periods=m_periods, m_person=m_person, m_wt=m_wt,
                           y_periods=y_periods, y_person=y_person, y_wt=y_wt)


@app.route("/budget/add", methods=["POST"])
def budget_add():
    category = request.form.get("category", "person")
    name = request.form.get("name", "").strip()
    period_type = request.form.get("period_type", "monthly")
    period_raw = request.form.get("period", "").strip()
    budget_amount = parse_amount(request.form.get("budget_amount", ""))

    if period_type == "monthly" and "-" in period_raw:
        y, m = period_raw.split("-")
        period = f"{m}/{y}"
    else:
        period = period_raw

    if not name or not period or budget_amount == "":
        return redirect(url_for("budget_manage"))

    b = {
        "id": str(uuid.uuid4())[:8],
        "category": category,
        "name": name,
        "period_type": period_type,
        "period": period,
        "budget_amount": float(budget_amount),
    }
    db_budget_upsert(b)
    return redirect(url_for("budget_manage"))


@app.route("/budget/delete/<budget_id>", methods=["POST"])
def budget_delete(budget_id):
    db_budget_delete(budget_id)
    return redirect(url_for("budget_manage"))


@app.route("/budget/template")
def budget_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows([
        ["name", "month", "year", "amount", "category"],
        ["สมชาย แสนดี", 5, 2026, 50000, "person"],
        ["สมหญิง แสนสวย", 5, 2026, 30000, "person"],
        ["IT", 5, 2026, 100000, "work_type"],
    ])
    content = "﻿" + output.getvalue()
    return Response(content, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=budget_template.csv"})


@app.route("/budget/upload", methods=["POST"])
def budget_upload():
    file = request.files.get("csv_file")
    if not file or not file.filename:
        return redirect(url_for("budget_manage"))
    content = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        name = (row.get("name") or "").strip()
        month = (row.get("month") or "").strip()
        year = (row.get("year") or "").strip()
        amount = parse_amount(row.get("amount") or "")
        category = (row.get("category") or "person").strip()
        if not name or not month or not year or amount == "":
            continue
        try:
            period = f"{int(month):02d}/{int(year)}"
        except ValueError:
            continue
        b = {
            "id": str(uuid.uuid4())[:8],
            "category": category if category in ("person", "work_type") else "person",
            "name": name,
            "period_type": "monthly",
            "period": period,
            "budget_amount": float(amount),
        }
        db_budget_upsert(b)
    return redirect(url_for("budget_manage"))


@app.route("/dashboard/budget")
def dashboard_budget():
    budgets = load_budgets()
    all_requests = load_all()
    active = [r for r in all_requests if r["status"] != "ยกเลิก" and r.get("amount") not in ("", None)]

    months = sorted(set(r["created_at"].strftime("%m/%Y") for r in active), reverse=True)
    years  = sorted(set(r["created_at"].strftime("%Y") for r in active), reverse=True)

    period_type = request.args.get("period_type", "monthly")
    period = request.args.get("period", months[0] if months else "")

    period_budgets = [b for b in budgets if b["period_type"] == period_type and b["period"] == period]

    def get_actual(category, name):
        total = 0
        for r in active:
            r_period = r["created_at"].strftime("%m/%Y") if period_type == "monthly" else r["created_at"].strftime("%Y")
            if r_period != period:
                continue
            if category == "person" and r["name"] == name and r.get("work_type") == "PERSON":
                total += float(r["amount"])
            elif category == "work_type" and r.get("work_type") == name:
                total += float(r["amount"])
        return total

    comparisons = []
    for b in period_budgets:
        actual = get_actual(b["category"], b["name"])
        budget_amt = float(b["budget_amount"])
        diff = budget_amt - actual
        pct = min((actual / budget_amt * 100) if budget_amt > 0 else 0, 100)
        comparisons.append({**b, "actual": actual, "diff": diff, "pct": round(pct, 1), "over": actual > budget_amt})

    return render_template("budget_dashboard.html",
                           comparisons=comparisons, months=months, years=years,
                           period_type=period_type, period=period, work_types=WORK_TYPES)


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
