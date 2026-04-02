"""
SlipSense Telegram Bot
======================
ส่ง Slip โอนเงิน → AI อ่านอัตโนมัติ → บันทึกรายรับ/รายจ่าย
ดู Dashboard ได้เลยใน Telegram

Setup:
  1. pip install -r requirements.txt
  2. copy .env.example → .env แล้วใส่ค่า
  3. python bot.py
"""

import os
import io
import json
import logging
import sqlite3
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─── Google Cloud Vision (OCR) ───────────────────────────────────────────────
try:
    from google.cloud import vision
    VISION_AVAILABLE = True
except ImportError:
    VISION_AVAILABLE = False

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
DASHBOARD_URL   = os.getenv("DASHBOARD_URL", "https://your-dashboard-url.com")
GOOGLE_CREDS    = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
DB_PATH         = Path("slipsense.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://your-dashboard-url.com")
GOOGLE_CREDS  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
DB_PATH       = Path("slipsense.db")

# ✅ เพิ่มบรรทัดนี้ — บอก Google SDK ให้ใช้ไฟล์ credentials
if GOOGLE_CREDS:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_CREDS
    
# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            type        TEXT    NOT NULL,   -- income / expense
            amount      REAL    NOT NULL,
            category    TEXT,
            description TEXT,
            bank        TEXT,
            slip_date   TEXT,
            created_at  TEXT    DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS budgets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            category    TEXT    NOT NULL,
            limit_amt   REAL    NOT NULL,
            month       TEXT    NOT NULL,   -- YYYY-MM
            UNIQUE(user_id, category, month)
        );
    """)
    con.commit()
    con.close()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row   # ทำให้ dict(row) ใช้ได้
    return con

# ─── HELPERS ──────────────────────────────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "อาหาร":       ["อาหาร","ร้าน","กิน","coffee","cafe","สตาร์บัคส์","grab food","food"],
    "เดินทาง":     ["grab","bolt","รถ","taxi","bts","mrt","น้ำมัน","parking","จอดรถ"],
    "ช้อปปิ้ง":    ["lazada","shopee","amazon","shop","mall","central","สินค้า"],
    "สุขภาพ":      ["โรงพยาบาล","ยา","คลินิก","หมอ","pharmacy","เวชภัณฑ์"],
    "ที่อยู่อาศัย":["เช่า","ค่าน้ำ","ค่าไฟ","อินเทอร์เน็ต","internet","true","ais","dtac"],
    "บันเทิง":     ["netflix","spotify","youtube","cinema","โรงหนัง","concert","เกม"],
    "เงินเดือน":   ["เงินเดือน","salary","โบนัส","bonus","ค่าจ้าง"],
    "รับโอน":      ["รับโอน","โอนเข้า","รับเงิน"],
}

BANK_COLORS = {
    "kbank":    "🟢 KBank (กสิกร)",
    "scb":      "🟣 SCB (ไทยพาณิชย์)",
    "ktb":      "🔵 KTB (กรุงไทย)",
    "bay":      "🟡 BAY (กรุงศรี)",
    "tmb":      "🔷 TTB (ทหารไทย)",
    "bbl":      "🔵 BBL (กรุงเทพ)",
    "promptpay":"💳 PromptPay",
}

def guess_category(text: str) -> str:
    text_lower = text.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return cat
    return "อื่นๆ"

def detect_bank(text: str) -> str:
    text_lower = text.lower()
    for key, name in BANK_COLORS.items():
        if key in text_lower:
            return name
    return "🏦 ไม่ระบุ"

def format_thb(amount: float) -> str:
    return f"฿{amount:,.2f}"

def this_month() -> str:
    return date.today().strftime("%Y-%m")

# ─── OCR: GOOGLE CLOUD VISION ─────────────────────────────────────────────────
def ocr_slip_google(image_bytes: bytes) -> dict:
    """อ่าน Slip ด้วย Google Cloud Vision — ฟรี 1,000 ครั้ง/เดือน"""
    if not VISION_AVAILABLE:
        raise ImportError("google-cloud-vision ไม่ได้ติดตั้ง")

    client = vision.ImageAnnotatorClient()
    image  = vision.Image(content=image_bytes)
    resp   = client.text_detection(image=image)

    if resp.error.message:
        raise RuntimeError(resp.error.message)

    full_text = resp.text_annotations[0].description if resp.text_annotations else ""
    return parse_slip_text(full_text)

def parse_slip_text(text: str) -> dict:
    """แยกข้อมูลจาก text ที่ได้จาก OCR"""
    import re

    # หายอดเงิน
    amount = 0.0
    money_patterns = [
        r"(?:จำนวน|ยอด|amount)[^\d]*([\d,]+\.?\d*)",
        r"([\d,]+\.\d{2})\s*(?:บาท|THB|baht)",
        r"(?:THB|บาท)\s*([\d,]+\.?\d*)",
    ]
    for pat in money_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                amount = float(m.group(1).replace(",", ""))
                break
            except ValueError:
                pass

    # หาวันที่
    slip_date = datetime.now().strftime("%d/%m/%Y %H:%M")
    date_patterns = [
        r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s*(\d{2}:\d{2})?",
        r"(\d{4}[/\-]\d{2}[/\-]\d{2})",
    ]
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            slip_date = m.group(0).strip()
            break

    # กำหนดประเภท
    tx_type = "expense"
    if any(kw in text for kw in ["รับโอน", "โอนเข้า", "รับเงิน", "เงินเดือน", "salary"]):
        tx_type = "income"

    return {
        "amount":      amount,
        "type":        tx_type,
        "category":    guess_category(text),
        "bank":        detect_bank(text),
        "slip_date":   slip_date,
        "description": text[:120].replace("\n", " "),
        "raw_text":    text,
    }

def mock_ocr(filename: str = "") -> dict:
    """Demo OCR เมื่อไม่มี Google Vision (สำหรับทดสอบ)"""
    import random
    demos = [
        {"amount": 350.0,   "type": "expense", "category": "อาหาร",        "bank": "🟢 KBank",     "slip_date": datetime.now().strftime("%d/%m/%Y %H:%M"), "description": "ร้านอาหาร สีลม"},
        {"amount": 220.0,   "type": "expense", "category": "เดินทาง",      "bank": "🟣 SCB",       "slip_date": datetime.now().strftime("%d/%m/%Y %H:%M"), "description": "Grab รถ"},
        {"amount": 45000.0, "type": "income",  "category": "เงินเดือน",    "bank": "💳 PromptPay", "slip_date": datetime.now().strftime("%d/%m/%Y %H:%M"), "description": "เงินเดือน เม.ย."},
        {"amount": 185.0,   "type": "expense", "category": "อาหาร",        "bank": "🟢 KBank",     "slip_date": datetime.now().strftime("%d/%m/%Y %H:%M"), "description": "Starbucks"},
        {"amount": 10000.0, "type": "expense", "category": "ที่อยู่อาศัย", "bank": "🟣 SCB",       "slip_date": datetime.now().strftime("%d/%m/%Y %H:%M"), "description": "ค่าเช่าบ้าน"},
    ]
    return random.choice(demos)

# ─── SUMMARY ──────────────────────────────────────────────────────────────────
def get_monthly_summary(user_id: int, month: str = None) -> dict:
    if not month:
        month = this_month()
    con = db()
    cur = con.cursor()

    cur.execute("""
        SELECT type, SUM(amount), category, COUNT(*)
        FROM transactions
        WHERE user_id = ? AND strftime('%Y-%m', created_at) = ?
        GROUP BY type, category
        ORDER BY SUM(amount) DESC
    """, (user_id, month))
    rows = cur.fetchall()
    con.close()

    income = 0.0
    expense = 0.0
    cat_expense = {}

    for row in rows:
        tx_type, total, cat, cnt = row
        if tx_type == "income":
            income += total
        else:
            expense += total
            cat_expense[cat] = cat_expense.get(cat, 0) + total

    return {
        "income":      income,
        "expense":     expense,
        "balance":     income - expense,
        "savings_pct": round((income - expense) / income * 100, 1) if income > 0 else 0,
        "categories":  cat_expense,
        "month":       month,
    }

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────
def main_keyboard(dashboard_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 เปิด Dashboard", web_app=WebAppInfo(url=dashboard_url)),
            InlineKeyboardButton("📋 สรุปเดือนนี้", callback_data="summary"),
        ],
        [
            InlineKeyboardButton("🎯 งบประมาณ", callback_data="budget"),
            InlineKeyboardButton("💡 AI แนะนำ",  callback_data="tip"),
        ],
    ])

# ✅ แก้ไข: ไม่รับ slip_data แล้ว ใช้ callback_data สั้นๆ แทน
def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ บันทึกรายการ", callback_data="save"),
            InlineKeyboardButton("❌ ยกเลิก",       callback_data="cancel"),
        ],
        [
            InlineKeyboardButton("✏️ แก้ไขหมวด", callback_data="edit_cat"),
        ],
    ])

# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"สวัสดีครับ {name}! 👋\n\n"
        "🤖 *SlipSense Bot* พร้อมช่วยจัดการการเงินคุณแล้ว!\n\n"
        "📸 *วิธีใช้:* ส่งรูป Slip โอนเงินมาเลย\n"
        "AI จะอ่านและบันทึกให้อัตโนมัติ ✨\n\n"
        "📌 *คำสั่งทั้งหมด:*\n"
        "  /dashboard — เปิดกราฟรายรับรายจ่าย\n"
        "  /summary — สรุปเดือนนี้\n"
        "  /budget — เช็คงบประมาณ\n"
        "  /tip — คำแนะนำออมเงิน\n"
        "  /list — ดูรายการล่าสุด + ลบรายการผิด\n"
        "  /help — วิธีใช้งาน",
        parse_mode="Markdown",
        reply_markup=main_keyboard(DASHBOARD_URL),
    )

async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *เปิด Dashboard* — กดปุ่มด้านล่างได้เลยครับ!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 เปิด Dashboard", web_app=WebAppInfo(url=DASHBOARD_URL)),
        ]]),
    )

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_monthly_summary(user_id)
    month_th = datetime.strptime(s["month"], "%Y-%m").strftime("%B %Y")

    cat_lines = ""
    for cat, amt in sorted(s["categories"].items(), key=lambda x: -x[1])[:5]:
        cat_lines += f"  • {cat}: {format_thb(amt)}\n"

    emoji_balance = "📈" if s["balance"] >= 0 else "📉"

    text = (
        f"📊 *สรุปการเงิน — {month_th}*\n"
        f"{'─'*30}\n"
        f"💚 รายรับ:     `{format_thb(s['income'])}`\n"
        f"❤️ รายจ่าย:   `{format_thb(s['expense'])}`\n"
        f"{emoji_balance} ยอดสุทธิ:   `{format_thb(s['balance'])}`\n"
        f"💜 อัตราออม:  `{s['savings_pct']}%`\n\n"
        f"🏷️ *หมวดรายจ่ายสูงสุด:*\n{cat_lines or '  ยังไม่มีข้อมูล'}"
    )
    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=main_keyboard(DASHBOARD_URL),
    )

async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_monthly_summary(user_id)

    DEFAULT_BUDGETS = {
        "อาหาร": 14600, "ที่อยู่อาศัย": 15000, "เดินทาง": 15000,
        "ช้อปปิ้ง": 7000, "สุขภาพ": 5000, "บันเทิง": 5000,
    }

    lines = []
    for cat, budget in DEFAULT_BUDGETS.items():
        spent = s["categories"].get(cat, 0)
        pct   = spent / budget * 100 if budget > 0 else 0
        if pct >= 100:
            bar = "🔴"
        elif pct >= 80:
            bar = "🟡"
        else:
            bar = "🟢"
        lines.append(f"{bar} {cat}: {format_thb(spent)} / {format_thb(budget)} ({pct:.0f}%)")

    text = "🎯 *งบประมาณเดือนนี้*\n" + "─" * 30 + "\n" + "\n".join(lines)
    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=main_keyboard(DASHBOARD_URL),
    )

async def cmd_tip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_monthly_summary(user_id)

    tips = [
        f"💡 คุณออมได้ *{s['savings_pct']}%* เดือนนี้ — เป้าหมายที่ดีคือ 20% ขึ้นไปครับ",
        "💡 ลองใช้กฎ *50/30/20* — 50% ค่าใช้จ่ายจำเป็น, 30% ส่วนตัว, 20% ออม",
        "💡 ออมก่อนใช้! โอนเงินออมทันทีที่ได้รับเงินเดือน แล้วค่อยใช้ที่เหลือ",
        f"💡 รายจ่ายที่สูงสุดของคุณคือ *{next(iter(s['categories']), 'ยังไม่มีข้อมูล')}* — ลองหาทางลดดูไหมครับ?",
    ]
    import random
    await update.message.reply_text(
        random.choice(tips), parse_mode="Markdown",
        reply_markup=main_keyboard(DASHBOARD_URL),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *วิธีใช้ SlipSense Bot*\n\n"
        "*1. ส่ง Slip*\n"
        "ถ่ายรูปหรือ Screenshot Slip แล้วส่งในแชทนี้\n"
        "Bot จะอ่านและถามยืนยันก่อนบันทึก\n\n"
        "*2. ดู Dashboard*\n"
        "กดปุ่ม 📊 หรือพิมพ์ /dashboard\n\n"
        "*3. คำสั่งด่วน*\n"
        "  /summary — สรุปรายรับรายจ่ายเดือนนี้\n"
        "  /budget — เช็คงบประมาณแต่ละหมวด\n"
        "  /tip — รับคำแนะนำการออมเงิน\n"
        "  /list — ดูรายการล่าสุด (พร้อมปุ่มลบ)\n\n"
        "💬 มีปัญหา? ติดต่อผู้พัฒนาได้เลยครับ",
        parse_mode="Markdown",
        reply_markup=main_keyboard(DASHBOARD_URL),
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """แสดง 10 รายการล่าสุด พร้อมปุ่มลบแต่ละรายการ"""
    user_id = update.effective_user.id
    con = db()
    rows = con.execute("""
        SELECT id, type, amount, category, description, created_at
        FROM transactions
        WHERE user_id = ?
        ORDER BY created_at DESC LIMIT 10
    """, (user_id,)).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            "📭 ยังไม่มีรายการครับ\n"
            "ส่ง Slip มาเลย — Bot จะบันทึกให้อัตโนมัติ! 📸",
            reply_markup=main_keyboard(DASHBOARD_URL),
        )
        return

    await update.message.reply_text(
        f"📋 *รายการล่าสุด 10 รายการ*\n"
        f"กดปุ่ม 🗑️ เพื่อลบรายการที่ไม่ต้องการ",
        parse_mode="Markdown",
    )

    for row in rows:
        row = dict(zip(["id","type","amount","category","description","created_at"], row))
        type_emoji = "💚" if row["type"] == "income" else "❤️"
        type_th    = "รายรับ" if row["type"] == "income" else "รายจ่าย"
        desc       = (row["description"] or "")[:45]
        date_str   = row["created_at"][:16]

        text = (
            f"{type_emoji} *{format_thb(row['amount'])}* — {row['category']}\n"
            f"📝 {desc}\n"
            f"📅 {date_str}  |  #{row['id']}"
        )
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑️ ลบรายการนี้", callback_data=f"del|{row['id']}")
            ]])
        )

# ─── SLIP HANDLER (รับรูปภาพ) ─────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 AI กำลังอ่าน Slip...")

    try:
        # ดาวน์โหลดรูปภาพ
        photo   = update.message.photo[-1]
        file    = await ctx.bot.get_file(photo.file_id)
        buf     = io.BytesIO()
        await file.download_to_memory(buf)
        image_bytes = buf.getvalue()

        # OCR
        if VISION_AVAILABLE and GOOGLE_CREDS:
            slip_data = ocr_slip_google(image_bytes)
        else:
            slip_data = mock_ocr()

        # ✅ แก้ไข: เก็บ slip_data ไว้ใน ctx.user_data แทนใส่ใน callback_data
        ctx.user_data["pending_slip"] = slip_data

        type_emoji = "💚" if slip_data["type"] == "income" else "❤️"
        type_th    = "รายรับ" if slip_data["type"] == "income" else "รายจ่าย"

        text = (
            f"✅ *อ่าน Slip สำเร็จ!*\n"
            f"{'─'*28}\n"
            f"🏦 ธนาคาร:   `{slip_data['bank']}`\n"
            f"📅 วันที่:    `{slip_data['slip_date']}`\n"
            f"💰 จำนวน:    `{format_thb(slip_data['amount'])}`\n"
            f"{type_emoji} ประเภท:    `{type_th}`\n"
            f"🏷️ หมวดหมู่: `{slip_data['category']}`\n"
            f"📝 รายละเอียด: `{slip_data['description'][:60]}`\n"
            f"{'─'*28}\n"
            "ยืนยันบันทึกรายการนี้ไหมครับ?"
        )

        await msg.edit_text(text, parse_mode="Markdown", reply_markup=confirm_keyboard())

    except Exception as e:
        log.error(f"Slip processing error: {e}")
        await msg.edit_text(
            "⚠️ อ่าน Slip ไม่ได้ครับ ลองส่งรูปที่ชัดขึ้นได้เลย\n"
            f"_(error: {str(e)[:80]})_",
            parse_mode="Markdown",
        )

# ─── CALLBACK HANDLER ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data

    await query.answer()

    if data == "summary":
        s        = get_monthly_summary(user_id)
        month_th = datetime.strptime(s["month"], "%Y-%m").strftime("%B %Y")
        emoji    = "📈" if s["balance"] >= 0 else "📉"
        await query.message.reply_text(
            f"📊 *{month_th}*\n"
            f"💚 รายรับ: `{format_thb(s['income'])}`\n"
            f"❤️ รายจ่าย: `{format_thb(s['expense'])}`\n"
            f"{emoji} คงเหลือ: `{format_thb(s['balance'])}`\n"
            f"💜 ออม: `{s['savings_pct']}%`",
            parse_mode="Markdown",
        )

    elif data == "budget":
        await cmd_budget(update, ctx)

    elif data == "tip":
        await cmd_tip(update, ctx)

    elif data == "cancel":
        ctx.user_data.pop("pending_slip", None)
        await query.message.edit_text("❌ ยกเลิกแล้วครับ")

    # ✅ แก้ไข: เปลี่ยนจาก "save|{json}" เป็น "save" แล้วดึงข้อมูลจาก ctx.user_data
    elif data == "save":
        try:
            slip = ctx.user_data.get("pending_slip")
            if not slip:
                await query.message.reply_text("⚠️ ไม่พบข้อมูล Slip กรุณาส่งรูปใหม่ครับ")
                return

            con = db()
            con.execute("""
                INSERT INTO transactions
                    (user_id, type, amount, category, description, bank, slip_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                slip["type"],
                slip["amount"],
                slip["category"],
                slip["description"][:60],
                slip["bank"],
                slip["slip_date"],
            ))
            con.commit()
            con.close()

            # ล้างข้อมูลหลังบันทึกแล้ว
            ctx.user_data.pop("pending_slip", None)

            type_emoji = "💚" if slip["type"] == "income" else "❤️"
            await query.message.edit_text(
                f"✅ *บันทึกแล้ว!*\n\n"
                f"{type_emoji} `{format_thb(slip['amount'])}` — {slip['category']}\n"
                f"🏦 {slip['bank']}",
                parse_mode="Markdown",
                reply_markup=main_keyboard(DASHBOARD_URL),
            )
        except Exception as e:
            log.error(f"Save error: {e}")
            await query.message.reply_text(f"⚠️ บันทึกไม่ได้: {e}")

    elif data == "edit_cat":
        cats = list(CATEGORY_KEYWORDS.keys()) + ["อื่นๆ"]
        buttons = [
            [InlineKeyboardButton(f"🏷️ {c}", callback_data=f"cat|{c}")]
            for c in cats
        ]
        await query.message.edit_reply_markup(InlineKeyboardMarkup(buttons))

    elif data.startswith("cat|"):
        new_cat = data[4:]
        if "pending_slip" in ctx.user_data:
            ctx.user_data["pending_slip"]["category"] = new_cat
        await query.message.reply_text(
            f"✏️ เปลี่ยนหมวดเป็น *{new_cat}* แล้วครับ",
            parse_mode="Markdown",
        )

    elif data.startswith("del|"):
        # ยืนยันลบ
        tx_id = int(data[4:])
        con = db()
        row = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        con.close()
        if row:
            row = dict(row)
            await query.message.reply_text(
                f"🗑️ *ยืนยันการลบ?*\n"
                f"รายการ: `{row['description'][:50]}`\n"
                f"จำนวน: `{format_thb(row['amount'])}`\n"
                f"วันที่: `{row['created_at'][:16]}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ ลบเลย", callback_data=f"confirm_del|{tx_id}"),
                    InlineKeyboardButton("❌ ยกเลิก", callback_data="cancel"),
                ]])
            )
        else:
            await query.message.reply_text("⚠️ ไม่พบรายการนี้ครับ")

    elif data.startswith("confirm_del|"):
        tx_id = int(data[12:])
        con = db()
        row = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        affected = con.execute("DELETE FROM transactions WHERE id=?", (tx_id,)).rowcount
        con.commit()
        con.close()
        if affected:
            row = dict(row) if row else {}
            await query.message.edit_text(
                f"🗑️ *ลบรายการแล้ว*\n`{row.get('description','')[:50]}`\n`{format_thb(row.get('amount',0))}`",
                parse_mode="Markdown",
                reply_markup=main_keyboard(DASHBOARD_URL),
            )
        else:
            await query.message.reply_text("⚠️ ลบไม่ได้ ไม่พบรายการนี้")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError("❌ กรุณาใส่ BOT_TOKEN ใน .env")

    init_db()
    log.info("🚀 SlipSense Bot เริ่มทำงาน...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("summary",   cmd_summary))
    app.add_handler(CommandHandler("budget",    cmd_budget))
    app.add_handler(CommandHandler("tip",       cmd_tip))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("list",      cmd_list))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("✅ Bot พร้อมรับ Slip แล้ว!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()