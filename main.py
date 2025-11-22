import logging
import os
import sys
import asyncio
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import FSInputFile, LabeledPrice, PreCheckoutQuery, ContentType
from pydub import AudioSegment
import asyncpg

# --- SOZLAMALAR ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "SIZNING_BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "CLICK_TOKEN") 
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@host/dbname") 
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
STICKER_ID = "AgACAgIAAxkBAAPMaSG1dUR9Eg3Q6COAMXZPxRm85FoAAjcNaxtepBFJPJ5VbDHiP7ABAAMCAAN5AAM2BA" # YANGI QO'SHILDI
DOWNLOAD_DIR = "converts"

# FORMATLAR, LIMITLAR
TARGET_FORMATS = ["MP3", "WAV", "FLAC", "OGG", "M4A", "AIFF"]
FORMAT_EXTENSIONS = {f: f.lower() for f in TARGET_FORMATS}
THROTTLE_CACHE = {} 
THROTTLE_LIMIT = 15 
LIMITS = {
    "free": {"daily": 3, "duration": 20},
    "plus": {"daily": 15, "duration": 120},
    "pro": {"daily": 30, "duration": 480}
}
# Asosiy narxlar (Chegirmasiz)
BASE_PRICE_PLUS = 15000 * 100
BASE_PRICE_PRO = 30000 * 100

db_pool = None
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- TIZIM FUNKSIYALARI ---

def clean_download_dir():
    if os.path.exists(DOWNLOAD_DIR):
        for filename in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                logging.error(f"Faylni o'chirishda xato {file_path}: {e}")

def apply_discount(base_price, discount_percent):
    discount_factor = 1 - (discount_percent / 100)
    return int(base_price * discount_factor)

# --- DATABASE MANTIQI (POSTGRESQL) ---

async def init_db():
    global db_pool
    logging.info("PostgreSQLga ulanmoqda...")
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                status TEXT DEFAULT 'free',
                sub_end_date TIMESTAMP,
                daily_usage INTEGER DEFAULT 0,
                last_usage_date DATE,
                referrer_id BIGINT DEFAULT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT REFERENCES users(telegram_id),
                amount INTEGER NOT NULL, 
                payload TEXT NOT NULL,
                payment_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Default chegirma 0
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('discount_percent', '0') ON CONFLICT (key) DO NOTHING"
        )
    logging.info("PostgreSQLga ulanish muvaffaqiyatli.")

async def get_setting(key):
    async with db_pool.acquire() as conn:
        record = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        return record['value'] if record else None

async def set_setting(key, value):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2",
            key, value
        )

async def get_user(telegram_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)

async def grant_referral_bonus(referrer_id):
    new_end_date = datetime.now() + timedelta(days=1)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'plus', sub_end_date = $1 WHERE telegram_id = $2 AND status != 'pro'", 
            new_end_date, referrer_id
        )

async def register_user(telegram_id, referrer_id=None):
    today = datetime.now().date()
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO users (telegram_id, last_usage_date) VALUES ($1, $2) ON CONFLICT (telegram_id) DO NOTHING", 
            telegram_id, today
        )
        if result == 'INSERT 0 1': 
            if referrer_id and referrer_id != telegram_id:
                referrer = await conn.fetchrow("SELECT telegram_id FROM users WHERE telegram_id = $1", referrer_id)
                if referrer:
                    await conn.execute("UPDATE users SET referrer_id = $1 WHERE telegram_id = $2", referrer_id, telegram_id)
                    await grant_referral_bonus(referrer_id)
                    try:
                        await bot.send_message(referrer_id, "🎁 **Tabriklaymiz!** Sizga **1 kunlik PLUS** obunasi berildi!")
                    except Exception: pass

async def check_limits(telegram_id):
    today = datetime.now().date()
    user = await get_user(telegram_id)
    
    if not user:
        await register_user(telegram_id)
        user = await get_user(telegram_id)
        if not user: return 'free', 0, LIMITS['free']['daily'], False

    status = user['status']
    sub_end = user['sub_end_date']
    usage = user['daily_usage']
    last_date = user['last_usage_date']

    if status in ['plus', 'pro'] and sub_end and datetime.now() > sub_end:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET status = 'free', sub_end_date = NULL WHERE telegram_id = $1", telegram_id)
        status = 'free'

    if last_date and last_date < today:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET daily_usage = 0, last_usage_date = $1 WHERE telegram_id = $2", today, telegram_id)
        usage = 0

    max_limit = LIMITS[status]['daily']
    is_limited = usage >= max_limit
    return status, usage, max_limit, is_limited

async def update_usage(telegram_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET daily_usage = daily_usage + 1 WHERE telegram_id = $1", telegram_id)

async def get_user_count():
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")

async def get_total_revenue():
    async with db_pool.acquire() as conn:
        total_tiyin = await conn.fetchval("SELECT SUM(amount) FROM payments")
        return (total_tiyin or 0) / 100

# --- STATES & KEYBOARDS ---

class ConverterState(StatesGroup):
    wait_audio = State()
    wait_format = State()

class AdminState(StatesGroup):
    wait_message = State()
    wait_discount = State() # YANGI STATE

def main_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎵 Konvertatsiya")
    kb.button(text="📊 Statistika")
    kb.button(text="🌟 Obuna olish")
    kb.button(text="🔗 Referal havolasi")
    kb.button(text="📢 Reklama")
    kb.adjust(1, 2, 1)
    return kb.as_markup(resize_keyboard=True)

def format_kb():
    kb = InlineKeyboardBuilder()
    for fmt in TARGET_FORMATS:
        kb.button(text=fmt, callback_data=f"fmt_{fmt}")
    kb.adjust(3)
    return kb.as_markup()

def admin_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📈 Statistika")
    kb.button(text="🏷 Chegirma o'rnatish") # YANGI TUGMA
    kb.button(text="✉️ Xabar yuborish")
    kb.button(text="❌ Asosiy menyu")
    kb.adjust(1, 2, 1)
    return kb.as_markup(resize_keyboard=True)

# --- HANDLERS ---

@dp.message(CommandStart())
async def start(message: types.Message):
    referrer_id = None
    if message.text and len(message.text.split()) > 1:
        try:
            referrer_id = int(message.text.split()[1])
            if referrer_id == message.from_user.id: referrer_id = None 
        except ValueError: referrer_id = None

    await register_user(message.from_user.id, referrer_id)
    await message.answer(f"Assalamu alaykum, {message.from_user.first_name}!\n🔘 [ ΛTOMIC ] taqdim etadi. \n🔘 [ ΛTOMIC • Λudio Convertor ] ga xush kelibsiz. \n 🌟 Plus  va  🚀 Pro bilan yanada keng imkoniyatlarga ega bo'ling. \n\n Foydalanish qoidalari (ToU) bilan tanishing: https://t.me/Atomic_Online_Services/5", reply_markup=main_kb())

@dp.message(F.text == "📢 Reklama")
async def ads_handler(message: types.Message):
    await message.answer(f"Reklama bo'yicha adminga murojaat qiling: @Al_Abdul_Aziz")
    
@dp.message(F.text == "📊 Statistika")
async def stats(message: types.Message):
    status, usage, max_limit, _ = await check_limits(message.from_user.id)
    max_dur = LIMITS[status]['duration']
    user_data = await get_user(message.from_user.id)
    referrer_id = user_data['referrer_id'] if user_data and user_data['referrer_id'] else "Yo'q"
    
    await message.answer(
        f"👤 **Profil:**\n🏷 Status: **{status.upper()}**\n🔋 Limit: **{usage}/{max_limit}**\n⏱ Maks. uzunlik: **{max_dur}s**\n🤝 Taklif qildi: **{referrer_id}**",
    )

@dp.message(F.text == "🔗 Referal havolasi")
async def send_referral_link(message: types.Message):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(f"👋 **Do'stlarni taklif qiling!**\nBonus: **1 kunlik PLUS obunasi**\n\n🔗 **Havola:**\n`{link}`", parse_mode="Markdown")

# --- TO'LOV ---
@dp.message(F.text == "🌟 Obuna olish")
async def buy_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    disc_str = await get_setting('discount_percent')
    disc = int(disc_str or 0)
    
    # Dinamik narxlar
    price_plus = apply_discount(BASE_PRICE_PLUS, disc) / 100
    price_pro = apply_discount(BASE_PRICE_PRO, disc) / 100
    
    kb.button(text=f"🌟 PLUS ({int(price_plus)} uzs)", callback_data="buy_plus")
    kb.button(text=f"🚀 PRO ({int(price_pro)} uzs)", callback_data="buy_pro")
    kb.adjust(1)
    
    msg = f"🎉 **{disc}% CHEGIRMA!**\n" if disc > 0 else ""
    await message.answer(f"📦 **Tariflar:**\n\n{msg}🌟 **PLUS**\n• 15 fayl, 2 daqiqa\n\n🚀 **PRO**\n• 30 fayl, 8 daqiqa", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("buy_"))
async def invoice(call: types.CallbackQuery):
    plan = call.data.split("_")[1]
    title = f"{plan.upper()} Obuna"
    payload = f"sub_{plan}"
    
    disc_str = await get_setting('discount_percent')
    disc = int(disc_str or 0)
    base = BASE_PRICE_PLUS if plan == "plus" else BASE_PRICE_PRO
    price = apply_discount(base, disc)
    
    try:
        await bot.send_invoice(call.message.chat.id, title, "Obuna", payload, PAYMENT_TOKEN, "UZS", [LabeledPrice(label="Obuna", amount=price)], start_parameter="sub_conv")
        await call.answer() 
    except Exception as e:
        await call.answer("❌ Xatolik yuz berdi (Token yoki narx).", show_alert=True)

@dp.pre_checkout_query()
async def checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def paid(message: types.Message):
    status = "plus" if "plus" in message.successful_payment.invoice_payload else "pro"
    end = datetime.now() + timedelta(days=31)
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (telegram_id) VALUES ($1) ON CONFLICT (telegram_id) DO NOTHING", message.from_user.id)
        await conn.execute("INSERT INTO payments (telegram_id, amount, payload) VALUES ($1, $2, $3)", message.from_user.id, message.successful_payment.total_amount, message.successful_payment.invoice_payload)
        await conn.execute("UPDATE users SET status = $1, sub_end_date = $2 WHERE telegram_id = $3", status, end, message.from_user.id)
    await message.answer(f"✅ To'lov muvaffaqiyatli! Status: **{status.upper()}**")

# --- KONVERTATSIYA ---
@dp.message(F.text == "🎵 Konvertatsiya")
async def req_audio(message: types.Message, state: FSMContext):
    status, usage, max_limit, is_limited = await check_limits(message.from_user.id)
    if is_limited: return await message.answer(f"😔 Limit tugadi ({usage}/{max_limit}). Ertaga keling yoki obuna oling! \nAytgancha, do'stingizni taklif qilsangiz 1 kunlik 🌟 PLUS obunasiga ega bo'lasiz")
    await message.answer("Faylni yuboring (Audio/Video).")
    await state.set_state(ConverterState.wait_audio)

# --- main.py (get_file funksiyasini TOPING va to'liq almashtiring) ---

# --- main.py (get_file funksiyasini TOPING va to'liq almashtiring) ---

@dp.message(ConverterState.wait_audio, F.content_type.in_([ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO, ContentType.DOCUMENT]))
async def get_file(message: types.Message, state: FSMContext):
    
    # 🟢 DEBUG 1: Handlerga kirdi.
    await message.answer("➡️ Handler ichiga kirdi, tekshirilmoqda...") 
    
    uid = message.from_user.id
    # 1. THROTTLING CHECK
    if uid in THROTTLE_CACHE and (time.time() - THROTTLE_CACHE[uid]) < THROTTLE_LIMIT:
        return await message.answer(f"✋ Juda tez. Iltimos, {THROTTLE_LIMIT} soniya kuting.") 
    THROTTLE_CACHE[uid] = time.time()
    
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

    # 2. FAYL TURINI XAVFSIZ ANIQLASH
    fid = None
    ext = None
    try:
        if message.audio: 
            fid, ext = message.audio.file_id, os.path.splitext(message.audio.file_name or "a.mp3")[-1]
        elif message.voice: 
            fid, ext = message.voice.file_id, ".ogg"
        elif message.video: 
            fid, ext = message.video.file_id, ".mp4"
        elif message.document: 
            fid, ext = message.document.file_id, os.path.splitext(message.document.file_name or "a.dat")[-1]
        
        if fid is None:
             # Agar kontent turi aniqlangan lekin fid olinmagan bo'lsa (Masalan, surat)
             error_msg = f"Noto'g'ri fayl turi aniqlandi! Content Type: {message.content_type}"
             return await message.answer(error_msg)

    except Exception as e:
        # Fayl ID/Ext olishdagi har qanday kutilmagan xato
        return await message.answer(f"❌ Fayl identifikatsiyasi xatosi: {e}") 

    path = os.path.join(DOWNLOAD_DIR, f"{fid}_in{ext}")
    
    # 🟢 DEBUG 3: Fayl turini to'g'ri aniqladi.
    await message.answer(f"📥 Yuklanmoqda... Tur: {ext}") 
    
    # 3. FAYLNI YUKLASH VA LIMIT TEKSHIRISH
    try:
        file = await bot.get_file(fid)
        await bot.download_file(file.file_path, path)
        
        # Limitlarni tekshirish
        status, _, _, _ = await check_limits(uid)
        
        # PyDub bilan uzunlikni tekshirish
        try:
            segment = AudioSegment.from_file(path)
            dur = len(segment) / 1000
        except Exception:
             os.remove(path)
             return await message.answer("❌ Fayl sifati past yoki buzilgan.")

        if dur > LIMITS[status]['duration']:
            os.remove(path)
            return await message.answer(f"⚠️ Limit: {LIMITS[status]['duration']}s. Fayl: {int(dur)}s")
            
    except Exception as e:
        # Yuklashdagi umumiy xato
        if os.path.exists(path): os.remove(path)
        return await message.answer(f"❌ Yuklashda Xatolik: {e}")

    # 4. FORMATNI TANLASHGA O'TISH
    await state.update_data(path=path)
    await message.answer("Formatni tanlang:", reply_markup=format_kb())
    await state.set_state(ConverterState.wait_format)
@dp.callback_query(ConverterState.wait_format, F.data.startswith("fmt_"))
async def process(call: types.CallbackQuery, state: FSMContext):
    fmt = call.data.split("_")[1]
    ext = FORMAT_EXTENSIONS[fmt]
    data = await state.get_data()
    in_path = data['path']
    out_path = in_path.replace("_in", f"_out.{ext}")
    
    await call.message.edit_text(f"⏳ {fmt} ga o'girilmoqda...")
    try:
        audio = AudioSegment.from_file(in_path)
        params = ["-acodec", "pcm_s16le"] if fmt in ["WAV", "FLAC", "AIFF"] else None
        audio.export(out_path, format=ext, parameters=params)
        
        res = FSInputFile(out_path)
        if fmt in ['MP4', 'OGG']: await bot.send_document(call.from_user.id, res, caption=f"✅ {fmt}")
        else: await bot.send_audio(call.from_user.id, res, caption=f"✅ {fmt}")
        # 2. Maxsus Stikerni yuborish (YANGI QISM)
        await bot.send_sticker(call.from_user.id, STICKER_ID)
        await update_usage(call.from_user.id)
        os.remove(out_path)
    except: await call.message.edit_text("❌ Konvertatsiya xatosi.")
    if os.path.exists(in_path): os.remove(in_path)
    await call.message.delete()
    await state.clear()

# --- ADMIN PANEL ---
@dp.message(Command('admin'))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID: return await message.answer("Admin emassiz.")
    await message.answer("🔑 Admin Panel", reply_markup=admin_kb())

@dp.message(F.text == "❌ Asosiy menyu")
async def back_to_main_menu(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    await message.answer("Menyu.", reply_markup=main_kb())

@dp.message(F.text == "📈 Statistika")
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    users = await get_user_count()
    rev = await get_total_revenue()
    disc = await get_setting('discount_percent')
    await message.answer(f"📊 Stats:\n👤 Userlar: {users}\n💰 Daromad: {rev} UZS\n🏷 Chegirma: {disc}%")

@dp.message(F.text == "🏷 Chegirma o'rnatish")
async def ask_discount(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    curr = await get_setting('discount_percent')
    await message.answer(f"Joriy chegirma: {curr}%\nYangi foizni yozing (0-100). 0 = o'chirish.", reply_markup=ReplyKeyboardBuilder().button(text="❌ Asosiy menyu").as_markup(resize_keyboard=True))
    await state.set_state(AdminState.wait_discount)

@dp.message(AdminState.wait_discount)
async def set_discount_handler(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if not message.text.isdigit(): return await message.answer("Raqam yozing.")
    perc = int(message.text)
    if not (0 <= perc <= 100): return await message.answer("0 dan 100 gacha bo'lsin.")
    
    await set_setting('discount_percent', str(perc))
    await state.clear()
    await message.answer(f"✅ Chegirma {perc}% ga o'zgartirildi.", reply_markup=admin_kb())

@dp.message(F.text == "✉️ Xabar yuborish")
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Xabar matnini yozing.", reply_markup=ReplyKeyboardBuilder().button(text="❌ Asosiy menyu").as_markup(resize_keyboard=True))
    await state.set_state(AdminState.wait_message)

@dp.message(AdminState.wait_message)
async def broadcast_send(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    await message.answer("Yuborilmoqda...")
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT telegram_id FROM users")
    count = 0
    for u in users:
        try:
            await bot.send_message(u['telegram_id'], message.text)
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"✅ Yuborildi: {count} ta", reply_markup=admin_kb())

async def main():
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    clean_download_dir()
    await init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    await dp.start_polling(bot)
# --- main.py (Eng oxirgi handlerlar qismiga qo'shing) ---
# --- main.py (Boshqa handlerlar tugagan joyga qo'shing) ---

@dp.message(F.content_type.in_([ContentType.PHOTO, ContentType.STICKER, ContentType.DOCUMENT]))
async def get_file_id_temp(message: types.Message):
    """Foydalanuvchi yuborgan rasm, stiker yoki hujjatning File ID'sini qaytaradi."""
    
    file_id = None
    file_type = "Noma'lum"

    if message.photo:
        # Eng yuqori sifatli rasmni olamiz (oxirgi element)
        file_id = message.photo[-1].file_id
        file_type = "RASM"
    elif message.sticker:
        file_id = message.sticker.file_id
        file_type = "STIKER"
    elif message.document:
        file_id = message.document.file_id
        file_type = "HUJJAT"
    else:
        return # Agar boshqa turdagi kontent bo'lsa, indamaymiz
        
    await message.answer(
        f"✅ Fayl turi: {file_type}\n\n🆔 Fayl ID:\n`{file_id}`\n\n⚠️ Eslatma: Bu xabarni olgandan so'ng, ushbu kodni o'chirishingiz kerak.", 
        parse_mode="Markdown"
    )
# Foydalanuvchi "🎵 Konvertatsiya" ni bosmasdan audio/fayl yuborsa, eslatma berish
@dp.message(F.content_type.in_([ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO, ContentType.DOCUMENT]))
async def remind_user_to_start(message: types.Message):
    # Bu handler faqat foydalanuvchi ConverterState.wait_audio holatida bo'lmasa ishlaydi.
    await message.answer(
        "Iltimos, avval asosiy menyudan **'🎵 Konvertatsiya'** tugmasini bosing, so'ngra faylni yuboring.",
        reply_markup=main_kb()
    )
    
if __name__ == "__main__":
    asyncio.run(main())
