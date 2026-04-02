"""
SlipSense — Railway Entry Point
================================
รัน bot.py + api.py พร้อมกันใน process เดียว
"""

import threading
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── รัน API server ในอีก thread ──────────────────────────────────────────────
def run_api():
    from http.server import HTTPServer
    # import handler จาก api.py
    sys.path.insert(0, str(Path(__file__).parent))
    from api import SlipSenseAPI
    port = int(os.getenv("PORT", 5000))
    server = HTTPServer(("0.0.0.0", port), SlipSenseAPI)
    log.info(f"🌐 API server เริ่มที่ port {port}")
    server.serve_forever()

# ─── รัน Telegram Bot ─────────────────────────────────────────────────────────
def run_bot():
    import asyncio
    from bot import main as bot_main
    log.info("🤖 Telegram Bot เริ่มทำงาน...")
    bot_main()

if __name__ == "__main__":
    # เริ่ม API ใน background thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()

    # รัน Bot ใน main thread
    run_bot()
