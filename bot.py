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
        semua_jadwal = load_jadwal_firebase()
        now = datetime.datetime.now(TZ)
        today_str = now.date().isoformat()
        
        ada_yang_diselesaikan = False
        
        for jid, item in semua_jadwal.items():
            # PERBAIKAN 1: Hapus tzinfo=TZ agar bisa dibandingkan dengan now.time()
            t_remind = datetime.time(item["hour"], item["minute"], 0) 
            
            if now.time() >= t_remind and item.get("last_completed") != today_str:
                item["last_completed"] = today_str  
                ada_yang_diselesaikan = True
                
                for job in context.application.job_queue.get_jobs_by_name(f"nag_{jid}"):
                    job.schedule_removal()
                    
        if ada_yang_diselesaikan:
            save_jadwal_firebase(semua_jadwal)
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="✅ Terima kasih! Semua pengingat yang tertunda telah ditandai selesai untuk hari ini."
            )

async def trigger_nag(context: ContextTypes.DEFAULT_TYPE):
    item = context.job.data
    semua_jadwal = load_jadwal_firebase()
    db_item = semua_jadwal.get(item['id'])
    
    today_str = datetime.date.today().isoformat()
    
    if db_item and db_item.get("last_completed") != today_str:
        msg = (
            f"⚠️ **PERHATIAN [{db_item['category'].upper()}]**\n"
            f"Jadwal **{db_item['title']}** belum ditandai selesai! (Peringatan diulang setiap 15 menit)"
        )
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    else:
        context.job.schedule_removal()

async def trigger_reminder(context: ContextTypes.DEFAULT_TYPE):
    item = context.job.data
    msg = (
        f"⏰ **PENGINGAT [{item['category'].upper()}]**\n"
        f"Waktunya untuk: **{item['title']}**.\n\n"
        f"Ketik **'sudah'** atau **'done'** jika sudah dilakukan."
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    
    for job in context.job_queue.get_jobs_by_name(f"nag_{item['id']}"):
        job.schedule_removal()
        
    context.job_queue.run_repeating(
        trigger_nag, 
        interval=900, 
        first=900, 
        data=item, 
        name=f"nag_{item['id']}"
    )

def schedule_jobs(app, item):
    # Mesin alarm APScheduler tetap wajib pakai tzinfo=TZ
    t_remind = datetime.time(item["hour"], item["minute"], 0, tzinfo=TZ)
    for job in app.job_queue.get_jobs_by_name(f"remind_{item['id']}"):
        job.schedule_removal()
    app.job_queue.run_daily(trigger_reminder, time=t_remind, data=item, name=f"remind_{item['id']}")

async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pesan = " ".join(context.args)
    try:
        waktu_str, sisa = pesan.split(" ", 1)
        jam, menit = map(int, waktu_str.split(":"))
        kategori, judul = sisa.split(",", 1)
        
        new_id = f"jadwal_{int(datetime.datetime.now().timestamp())}"
        item = {
            "id": new_id,
            "category": kategori.strip(),
            "title": judul.strip(),
            "hour": jam,
            "minute": menit,
            "last_completed": "" 
        }
        
        semua_jadwal = load_jadwal_firebase()
        semua_jadwal[new_id] = item
        save_jadwal_firebase(semua_jadwal)
        
        schedule_jobs(context.application, item)
        await update.message.reply_text(f"✅ Jadwal '{item['title']}' ditambahkan permanen untuk {waktu_str} WIB.")
    except Exception as e:
        print(f"Error parsing command: {e}")
        await update.message.reply_text("❌ Format: `/tambah HH:MM Kategori, Judul`", parse_mode="Markdown")

async def hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pesan = " ".join(context.args).strip()
    if not pesan:
        await update.message.reply_text("❌ Format: `/hapus Judul Pengingat`\nContoh: `/hapus Obat Malam`", parse_mode="Markdown")
        return

    semua_jadwal = load_jadwal_firebase()
    id_to_delete = None
    
    for jid, item in semua_jadwal.items():
        if item['title'].lower() == pesan.lower():
            id_to_delete = jid
            break
            
    if id_to_delete:
        del semua_jadwal[id_to_delete]
        save_jadwal_firebase(semua_jadwal)
            
        current_jobs_remind = context.application.job_queue.get_jobs_by_name(f"remind_{id_to_delete}")
        current_jobs_nag = context.application.job_queue.get_jobs_by_name(f"nag_{id_to_delete}")
        for job in current_jobs_remind + current_jobs_nag:
            job.schedule_removal()
            
        await update.message.reply_text(f"✅ Jadwal '{pesan}' berhasil dihapus secara permanen.")
    else:
        await update.message.reply_text(f"❌ Jadwal dengan judul '{pesan}' tidak ditemukan.")

async def list_jadwal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    semua_jadwal = load_jadwal_firebase()
    
    if not semua_jadwal:
        await update.message.reply_text("📭 Saat ini belum ada jadwal pengingat yang terdaftar.")
        return

    jadwal_urut = sorted(semua_jadwal.values(), key=lambda x: (x['hour'], x['minute']))
    now = datetime.datetime.now(TZ)
    today_str = now.date().isoformat()
    
    pesan = "📋 **STATUS PENGINGAT HARI INI:**\n\n"
    for idx, item in enumerate(jadwal_urut, 1):
        jam_str = f"{item['hour']:02d}:{item['minute']:02d}"
        
        # PERBAIKAN 2: Hapus tzinfo=TZ di sini juga
        t_remind = datetime.time(item["hour"], item["minute"], 0)
        
        if item.get("last_completed") == today_str:
            status_teks = "✅ Sudah Selesai"
        elif now.time() >= t_remind:
            status_teks = "⏳ Tertunda (Menunggu Konfirmasi)"
        else:
            status_teks = "💤 Belum Waktunya"
            
        pesan += f"{idx}. **{jam_str} WIB** | {item['category']} - {item['title']}\n   Status: {status_teks}\n\n"
    
    await update.message.reply_text(pesan, parse_mode="Markdown")

# --- SERVER PALSU UNTUK MENGAKALI RENDER ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type','text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        pass

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()
# -------------------------------------------

def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mark_done))
    app.add_handler(CommandHandler("tambah", tambah))
    app.add_handler(CommandHandler("hapus", hapus))
    app.add_handler(CommandHandler(["list", "jadwal"], list_jadwal))

    semua_jadwal = load_jadwal_firebase()
    for jid, item in semua_jadwal.items():
        schedule_jobs(app, item)

    threading.Thread(target=run_dummy_server, daemon=True).start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app.run_polling()

if __name__ == '__main__':
    main()
