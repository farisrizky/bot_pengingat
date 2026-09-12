import os
import datetime
import requests
import threading
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
FIREBASE_URL = os.environ.get("FIREBASE_URL")
TZ = datetime.timezone(datetime.timedelta(hours=7))
STATUS_JADWAL = {}

def load_jadwal_firebase():
    if not FIREBASE_URL:
        return {}
    try:
        response = requests.get(f"{FIREBASE_URL}jadwal.json")
        if response.status_code == 200 and response.json():
            return response.json()
    except Exception:
        pass
    return {}

def save_jadwal_firebase(jadwal_dict):
    if not FIREBASE_URL:
        return
    try:
        requests.put(f"{FIREBASE_URL}jadwal.json", json=jadwal_dict)
    except Exception:
        pass

async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower().strip()
    if text in ["done", "sudah", "ok", "siap", "selesai"]:
        pending = [jid for jid, st in STATUS_JADWAL.items() if not st]
        if pending:
            for jid in pending:
                STATUS_JADWAL[jid] = True
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="✅ Terima kasih! Pengingat aktif telah ditandai selesai."
            )

async def trigger_reminder(context: ContextTypes.DEFAULT_TYPE):
    item = context.job.data
    STATUS_JADWAL[item['id']] = False
    msg = (
        f"⏰ **PENGINGAT [{item['category'].upper()}]**\n"
        f"Waktunya untuk: **{item['title']}**.\n\n"
        f"Ketik **'sudah'** atau **'done'** jika sudah dilakukan."
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

async def trigger_nag(context: ContextTypes.DEFAULT_TYPE):
    item = context.job.data
    if not STATUS_JADWAL.get(item['id'], True):
        msg = (
            f"⚠️ **PERHATIAN [{item['category'].upper()}]**\n"
            f"Jadwal **{item['title']}** belum ditandai selesai!"
        )
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

def schedule_jobs(app, item):
    t_remind = datetime.time(item["hour"], item["minute"], 0, tzinfo=TZ)
    today = datetime.date.today()
    nag_dt = datetime.datetime.combine(today, t_remind) + datetime.timedelta(minutes=30)
    t_nag = nag_dt.time()
    
    current_jobs = app.job_queue.get_jobs_by_name(f"remind_{item['id']}")
    for job in current_jobs:
        job.schedule_removal()
    
    app.job_queue.run_daily(trigger_reminder, time=t_remind, data=item, name=f"remind_{item['id']}")
    app.job_queue.run_daily(trigger_nag, time=t_nag, data=item, name=f"nag_{item['id']}")

async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pesan = " ".join(context.args)
    try:
        waktu_str, sisa = pesan.split(" ", 1)
        jam, menit = map(int, waktu_str.split(":"))
        kategori, judul = sisa.split("-", 1)
        
        new_id = f"jadwal_{int(datetime.datetime.now().timestamp())}"
        item = {
            "id": new_id,
            "category": kategori.strip(),
            "title": judul.strip(),
            "hour": jam,
            "minute": menit
        }
        
        semua_jadwal = load_jadwal_firebase()
        semua_jadwal[new_id] = item
        save_jadwal_firebase(semua_jadwal)
        
        STATUS_JADWAL[new_id] = True
        schedule_jobs(context.application, item)
        
        await update.message.reply_text(f"✅ Jadwal '{item['title']}' ditambahkan permanen untuk {waktu_str} WIB.")
    except Exception:
        await update.message.reply_text("❌ Format: `/tambah HH:MM Kategori - Judul`", parse_mode="Markdown")

# --- SERVER PALSU UNTUK MENGAKALI RENDER ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type','text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        pass # Biar log server gak nyampah kepanjangan

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()
# -------------------------------------------

def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mark_done))
    app.add_handler(CommandHandler("tambah", tambah))

    semua_jadwal = load_jadwal_firebase()
    for jid, item in semua_jadwal.items():
        STATUS_JADWAL[item['id']] = True
        schedule_jobs(app, item)

    # 1. Nyalakan web server palsu di background
    threading.Thread(target=run_dummy_server, daemon=True).start()

    # 2. Fix error event loop dari Python 3.14
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 3. Jalankan bot telegram
    app.run_polling()

if __name__ == '__main__':
    main()
