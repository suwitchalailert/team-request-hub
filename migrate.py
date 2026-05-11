"""
รัน script นี้ครั้งเดียวเพื่อย้ายข้อมูลจาก data.json ไป Supabase

วิธีใช้:
  set SUPABASE_URL=https://xxxx.supabase.co
  set SUPABASE_KEY=your-anon-key
  python migrate.py
"""
import json
import os
from supabase import create_client

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    print("ERROR: กรุณาตั้งค่า SUPABASE_URL และ SUPABASE_KEY ก่อนรัน")
    exit(1)

sb = create_client(url, key)

with open("data.json", "r", encoding="utf-8") as f:
    records = json.load(f)

for r in records:
    row = {k: v for k, v in r.items()}
    row["amount"] = r["amount"] if r.get("amount") not in ("", None) else None
    sb.table("requests").upsert(row).execute()
    print(f"  ✓ {r['name']} — #{r['id']}")

print(f"\nเสร็จสิ้น: ย้ายข้อมูล {len(records)} รายการ")
