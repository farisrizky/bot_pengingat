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

# Helper to ensure the Firebase URL is valid even if it lacks a trailing slash
def get_fb_url(endpoint="jadwal.json"):
    if not FIREBASE_URL:
        return ""
    base = FIREBASE_URL if FIREBASE_URL.endswith("/") else f"{FIREBASE_URL}/"
    return base + endpoint

def load_jadwal_firebase():
    url = get_fb_url()
    if not url:
        return {}
    try:
        # Added timeout to prevent Render server freezes
        response = requests.get(url, timeout=10)
        if response.status_code == 200 and response.json():
            return response.json()
    except Exception as e:
        print(f"Error loading Firebase: {e}")
    return {}

def save_jadwal_firebase(jadwal_dict):
    url = get_fb_url()
    if not url:
        return
    try:
        requests.put(url, json=jadwal_dict, timeout=10)
    except Exception as e:
        print(f"Error saving Firebase: {e}")

async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower().strip()
    trigger_words = ["done", "sudah", "ok", "siap", "selesai"]
    
    # Check if the message starts with a trigger word
    used_trigger = None
    for word in trigger_words:
        if text == word or text.startswith(word + " "):
            used_trigger = word
            break
            
    if not used_trigger:
        return

    semua_jadwal = load_jadwal_firebase()
    now = datetime.datetime.now(TZ)
    today_str = now.date().isoformat()
    
    # Find all PENDING schedules (time has passed, but not marked done today)
    pending_schedules = []
    for jid, item in semua_jadwal.items():
        try:
            t_remind = datetime.time(item["hour"], item["minute"], 0) 
            if now.time() >= t_remind and item.get("last_completed") != today_str:
                pending_schedules.append((jid, item))
        except ValueError:
            continue # Skip corrupted data
            
    if not pending_schedules:
        return 
        
    target_jid = None
    target_item = None
    
    # Extract specific keywords from the user (e.g., "done obat" -> keyword = "obat")
    keyword = text[len(used_trigger):].strip()
    
    if keyword:
        # User specified a schedule
        for jid, item in pending_schedules:
            if keyword in item['title'].lower() or keyword in item['category'].lower():
                target_jid = jid
                target_item = item
                break
                
        if not target_jid:
            await update.message.reply_text(f"❌ Tidak ada pengingat tertunda yang mengandung kata '{keyword}'.")
            return
    else:
        # User only typed "done"
        if len(pending_schedules) == 1:
            target_jid, target_item = pending_schedules[0]
        else:
            # If >1 pending, ask user to be specific
            msg = "⚠️ **Ada beberapa pengingat yang tertunda:**\n"
            for i, (jid, item) in enumerate(pending_schedules, 1):
                msg += f"{i}. {item['title']} [{item['category']}]\n"
            
            contoh = pending_schedules[0][1]['title'].split()[0]
            msg += f"\nTolong sebutkan spesifik. Contoh: `{used_trigger} {contoh}`"
            await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode="Markdown")
            return

    # Mark as completed
    if target_jid and target_item:
        target_item["last_completed"] = today_str  
        save_jadwal_firebase(semua_jadwal)
        
        # Remove nagging ONLY for the completed schedule
        for job in context.application.job_queue.get_jobs_by_name(f"nag_{target_jid}"):
            job.schedule_removal()
            
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=f"✅ Terima kasih! Pengingat **'{target_item['title']}'** telah ditandai selesai untuk hari ini.",
            parse_mode="Markdown"
        )

async def trigger_nag(context: ContextTypes.DEFAULT_TYPE):
    item = context.job.data
    semua_jadwal = load_jadwal_firebase()
    db_item = semua_jadwal.get(item['id'])
    
    today_str = datetime.datetime.now(TZ).date().isoformat()
    
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
        f"Ketik **'sudah {item['title'].split()[0]}'** jika sudah dilakukan."
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
    # Crash-proof wrapper: ignore schedules with invalid time formatting
    try:
        t_remind = datetime.time(item["hour"], item["minute"], 0, tzinfo=TZ)
        for job in app.job_queue.get_jobs_by_name(f"remind_{item['id']}"):
            job.schedule_removal()
        app.job_queue.run_daily(trigger_reminder, time=t_remind, data=item, name=f"remind_{item['id']}")
    except ValueError as e:
        print(f"Skipping invalid schedule {item.get('id')} - {item.get('title')}: {e}")

async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pesan = " ".join(context.args)
    try:
        waktu_str, sisa = pesan.split(" ", 1)
        jam, menit = map(int, waktu_str.split(":"))
        
        # Validation to prevent users from adding corrupt times
        if not (0 <= jam <= 23) or not (0 <= menit <= 59):
            await update.message.reply_text("❌ Format waktu salah! Jam harus 00-23 dan menit 00-59.")
            return
            
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

    jadwal_urut = sorted(semua_jadwal.values(), key=lambda x: (x.get('hour', 0), x.get('minute', 0)))
    now = datetime.datetime.now(TZ)
    today_str = now.date().isoformat()
    
    pesan = "📋 **STATUS PENGINGAT HARI INI:**\n\n"
    for idx, item in enumerate(jadwal_urut, 1):
        try:
            jam_str = f"{item['hour']:02d}:{item['minute']:02d}"
            t_remind = datetime.time(item["hour"], item["minute"], 0)
            
            if item.get("last_completed") == today_str:
                status_teks = "✅ Sudah Selesai"
            elif now.time() >= t_remind:
                status_teks = "⏳ Tertunda (Ketik: `done " + item['title'].split()[0] + "`)"
            else:
                status_teks = "💤 Belum Waktunya"
                
            pesan += f"{idx}. **{jam_str} WIB** | {item['category']} - {item['title']}\n   Status: {status_teks}\n\n"
        except (ValueError, KeyError):
            pesan += f"{idx}. ⚠️ Data Rusak - Hapus dan buat ulang dari Firebase\n\n"
    
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
