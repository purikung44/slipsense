"""
SlipSense API Server
====================
รัน api.py คู่กับ bot.py เพื่อให้ Dashboard ดึงข้อมูลจริงจาก database
"""

import sqlite3
import json
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DB_PATH = Path("slipsense.db")
PORT    = 5000

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def this_month():
    return date.today().strftime("%Y-%m")

# ─── ดึงข้อมูลจาก DB ────────────────────────────────────────────────────────

def get_transactions(limit=100, search=""):
    con = db()
    if search:
        rows = con.execute("""
            SELECT * FROM transactions
            WHERE description LIKE ? OR category LIKE ? OR bank LIKE ?
            ORDER BY created_at DESC LIMIT ?
        """, (f"%{search}%", f"%{search}%", f"%{search}%", limit)).fetchall()
    else:
        rows = con.execute("""
            SELECT * FROM transactions
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def get_summary(month=None):
    if not month:
        month = this_month()
    con = db()
    rows = con.execute("""
        SELECT type, category, SUM(amount) as total, COUNT(*) as cnt
        FROM transactions
        WHERE strftime('%Y-%m', created_at) = ?
        GROUP BY type, category
    """, (month,)).fetchall()
    con.close()

    income, expense, categories = 0.0, 0.0, {}
    for r in rows:
        if r["type"] == "income":
            income += r["total"]
        else:
            expense += r["total"]
            categories[r["category"]] = round(r["total"], 2)

    return {
        "month":       month,
        "income":      round(income, 2),
        "expense":     round(expense, 2),
        "balance":     round(income - expense, 2),
        "savings_pct": round((income - expense) / income * 100, 1) if income > 0 else 0,
        "categories":  categories,
    }

def get_monthly_trend(months=6):
    con = db()
    rows = con.execute("""
        SELECT strftime('%Y-%m', created_at) as month,
               type, SUM(amount) as total
        FROM transactions
        GROUP BY month, type
        ORDER BY month DESC LIMIT ?
    """, (months * 2,)).fetchall()
    con.close()

    data = {}
    for r in rows:
        m = r["month"]
        if m not in data:
            data[m] = {"month": m, "income": 0.0, "expense": 0.0}
        data[m][r["type"]] = round(r["total"], 2)

    return sorted(data.values(), key=lambda x: x["month"])

def get_weekly(month=None):
    """แบ่งรายจ่ายเดือนนี้เป็น 4 สัปดาห์"""
    if not month:
        month = this_month()
    con = db()
    rows = con.execute("""
        SELECT created_at, amount, type
        FROM transactions
        WHERE strftime('%Y-%m', created_at) = ? AND type = 'expense'
    """, (month,)).fetchall()
    con.close()

    weeks = [0.0, 0.0, 0.0, 0.0]
    for r in rows:
        try:
            d = datetime.fromisoformat(r["created_at"])
            week_num = min((d.day - 1) // 7, 3)
            weeks[week_num] += r["amount"]
        except Exception:
            pass

    return [round(w, 2) for w in weeks]

def delete_transaction(tx_id):
    con = db()
    cur = con.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    affected = cur.rowcount
    con.commit()
    con.close()
    return affected > 0

# ─── HTTP Handler ────────────────────────────────────────────────────────────

# alias สำหรับ main.py import
SlipSenseAPI = None  # กำหนดด้านล่างหลัง class

class APIHandler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)
        search = params.get("search", [""])[0]

        if path == "/api/transactions":
            self.send_json(get_transactions(search=search))

        elif path == "/api/summary":
            month = params.get("month", [None])[0]
            self.send_json(get_summary(month))

        elif path == "/api/trend":
            self.send_json(get_monthly_trend())

        elif path == "/api/weekly":
            self.send_json(get_weekly())

        elif path == "/api/all":
            self.send_json({
                "transactions": get_transactions(search=search),
                "summary":      get_summary(),
                "trend":        get_monthly_trend(),
                "weekly":       get_weekly(),
            })

        elif path == "/health":
            self.send_json({"status": "ok", "time": datetime.now().isoformat()})

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        # DELETE /api/transaction/123
        path = self.path.split("?")[0]
        if path.startswith("/api/transaction/"):
            try:
                tx_id = int(path.split("/")[-1])
                ok = delete_transaction(tx_id)
                if ok:
                    self.send_json({"success": True, "message": f"ลบรายการ #{tx_id} แล้ว"})
                else:
                    self.send_json({"success": False, "message": "ไม่พบรายการนี้"}, 404)
            except (ValueError, IndexError):
                self.send_json({"error": "ID ไม่ถูกต้อง"}, 400)
        else:
            self.send_json({"error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        print(f"[API] {self.address_string()} {fmt % args}")

# export alias ให้ main.py ใช้ได้
SlipSenseAPI = APIHandler

# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_PATH.exists():
        print("⚠️  ยังไม่มี database — รัน bot.py ก่อนแล้วส่ง Slip สักอัน")
    else:
        print(f"✅ SlipSense API พร้อมแล้ว → http://localhost:{PORT}")
        print(f"   GET    /api/all              — ทุกอย่างในครั้งเดียว")
        print(f"   GET    /api/transactions     — รายการที้งหมด")
        print(f"   GET    /api/summary          — สรุปเดือนนี้")
        print(f"   GET    /api/trend            — แนวโน้มรายเดือน")
        print(f"   GET    /api/weekly           — รายจ่ายรายสัปดาห์")
        print(f"   DELETE /api/transaction/<id> — ลบรายการ")

    server = HTTPServer(("0.0.0.0", PORT), APIHandler)
    print(f"\n🚀 API Server รันอยู่ที่ http://localhost:{PORT}")
    server.serve_forever()
