import os
import datetime
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# Mengambil variabel lingkungan dari sistem operasi (atau Render nantinya)
TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
FIREBASE_URL = os.environ.get("FIREBASE_URL")

# Standarisasi zona waktu (WIB = UTC+7)
TZ = datetime.timezone(datetime.timedelta(hours=7))

# Dictionary sementara di memori (RAM) untuk melacak status "selesai/belum" harian
STATUS_JADWAL = {}

def load_jadwal_firebase():
    """Mengambil seluruh data jadwal dari Firebase."""
    response = requests.get(f"{FIREBASE_URL}jadwal.json")
    if response.status_code == 200 and response.json():
        return response.json()
    return {}

def save_jadwal_firebase(jadwal_dict):
    """Menimpa data jadwal di Firebase dengan data (dictionary) yang baru."""
    requests.put(f"{FIREBASE_URL}jadwal.json", json=jadwal_dict)

async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler pendeteksi konfirmasi penyelesaian dari user di grup."""
    text = update.message.text.lower().strip()
    if text in ["done", "sudah", "ok", "siap", "selesai"]:
        # Cari semua ID jadwal yang statusnya False (belum selesai)
        pending = [jid for jid, st in STATUS_JADWAL.items() if not st]
        
        if pending:
            for jid in pending:
                STATUS_JADWAL[jid] = True
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="✅ Terima kasih! Pengingat aktif telah ditandai selesai."
            )

async def trigger_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Fungsi yang dieksekusi saat waktu pengingat tiba."""
    item = context.job.data
    STATUS_JADWAL[item['id']] = False  # Tandai butuh konfirmasi
    
    msg = (
        f"⏰ **PENGINGAT [{item['category'].upper()}]**\n"
        f"Waktunya untuk: **{item['title']}**.\n\n"
        f"Ketik **'sudah'** atau **'done'** jika sudah dilakukan."
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

async def trigger_nag(context: ContextTypes.DEFAULT_TYPE):
    """Fungsi pengecekan ulang (nag) 30 menit setelah reminder."""
    item = context.job.data
    # Cek apakah status masih False (belum dikonfirmasi)
    if not STATUS_JADWAL.get(item['id'], True):
        msg = (
            f"⚠️ **PERHATIAN [{item['category'].upper()}]**\n"
            f"Jadwal **{item['title']}** belum ditandai selesai!"
        )
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

def schedule_jobs(app, item):
    """Meregistrasikan fungsi pengingat ke dalam job queue."""
    t_remind = datetime.time(item["hour"], item["minute"], 0, tzinfo=TZ)
    
    # Hitung waktu untuk nag (30 menit setelah remind)
    today = datetime.date.today()
    nag_dt = datetime.datetime.combine(today, t_remind) + datetime.timedelta(minutes=30)
    t_nag = nag_dt.time()
    
    # Hapus job lama dari memori jika id tersebut sudah ada
    current_jobs = app.job_queue.get_jobs_by_name(f"remind_{item['id']}")
    for job in current_jobs:
        job.schedule_removal()
    
    app.job_queue.run_daily(trigger_reminder, time=t_remind, data=item, name=f"remind_{item['id']}")
    app.job_queue.run_daily(trigger_nag, time=t_nag, data=item, name=f"nag_{item['id']}")

async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk command /tambah dari Telegram."""
    pesan = " ".join(context.args)
    try:
        # Memisahkan string "/tambah 08:00 Obat - Vitamin C"
        waktu_str, sisa = pesan.split(" ", 1)
        jam, menit = map(int, waktu_str.split(":"))
        kategori, judul = sisa.split("-", 1)
        
        # Generate ID unik berdasarkan timestamp
        new_id = f"jadwal_{int(datetime.datetime.now().timestamp())}"
        item = {
            "id": new_id,
            "category": kategori.strip(),
            "title": judul.strip(),
            "hour": jam,
            "minute": menit
        }
        
        # 1. Tarik state saat ini dari Database
        semua_jadwal = load_jadwal_firebase()
        # 2. Mutasi state
        semua_jadwal[new_id] = item
        # 3. Push state baru ke Database
        save_jadwal_firebase(semua_jadwal)
        
        # Update state sementara di RAM
        STATUS_JADWAL[new_id] = True
        schedule_jobs(context.application, item)
        
        await update.message.reply_text(f"✅ Jadwal '{item['title']}' ditambahkan permanen untuk {waktu_str} WIB.")
    except Exception:
        await update.message.reply_text("❌ Format: `/tambah HH:MM Kategori - Judul`", parse_mode="Markdown")

def main():
    """Fungsi inisialisasi aplikasi."""
    app = Application.builder().token(TOKEN).build()
    
    # Registrasi handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mark_done))
    app.add_handler(CommandHandler("tambah", tambah))

    # Inisialisasi awal: Muat semua jadwal dari Firebase dan masukkan ke Job Queue
    semua_jadwal = load_jadwal_firebase()
    for jid, item in semua_jadwal.items():
        STATUS_JADWAL[item['id']] = True
        schedule_jobs(app, item)

    print("Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
