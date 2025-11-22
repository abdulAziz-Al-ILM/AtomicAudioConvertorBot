import logging
import os
import sys
import asyncio
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
# TUZATISH: ContentType qo'shildi 👇
from aiogram.types import FSInputFile, LabeledPrice, PreCheckoutQuery, ContentType
import aiosqlite
from pydub import AudioSegment

# --- SOZLAMALAR (Railway Variables) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "SIZNING_BOT_TOKEN")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "CLICK_TOKEN") 
# Agar admin kerak bo'lsa
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

DB_NAME = "converter_bot.db"
DOWNLOAD_DIR = "converts"

# FORMATLAR
TARGET_FORMATS = ["MP3", "WAV", "FLAC", "OGG", "M4A", "AIFF"]
FORMAT_EXTENSIONS = {f: f.lower() for f in TARGET_FORMATS}

# XAVFSIZLIK
THROTTLE_CACHE = {} 
THROTTLE_LIMIT = 15 

# LIMITLAR VA NARXLAR
LIMITS = {
    "free": {"daily": 5, "duration": 20},     # 20 soniya
    "plus": {"daily": 15, "duration": 120},   # 2 daqiqa
    "pro": {"daily": 30, "duration": 480}     # 8 daqiqa
}

PRICE_PLUS = 15000 * 100
PRICE_PRO = 30000 * 100

# --- DATABASE ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'free',
                sub_end_date TEXT,
                daily_usage INTEGER DEFAULT 0,
                last_usage_date TEXT
            )
        """)
        await db.commit()

async def get_user(telegram_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            return await cursor.fetchone()

async def register_user(telegram_id):
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO users (telegram_id, last_usage_date) VALUES (?, ?)", (telegram_id, today))
            await db.commit()
        except aiosqlite.IntegrityError: pass

async def check_limits(telegram_id):
    today = datetime.now().date().isoformat()
    user = await get_user(telegram_id)
    if not user: return 'free', 0, LIMITS['free']['daily'], False
    
    status, sub_end, usage, last_date = user[1], user[2], user[3], user[4]
    
    # Obuna muddati tugaganini tekshirish
    if status in ['plus', 'pro'] and sub_end:
        if datetime.now() > datetime.fromisoformat(sub_end):
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET status = 'free', sub_end_date = NULL WHERE telegram_id = ?", (telegram_id,))
                await db.commit()
            status = 'free'

    # Kunlik limitni yangilash
    if last_date != today:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET daily_usage = 0, last_usage_date = ? WHERE telegram_id = ?", (today, telegram_id))
            await db.commit()
        usage = 0

    max_limit = LIMITS[status]['daily']
    is_limited = usage >= max_limit
    return status, usage, max_limit, is_limited

async def update_usage(telegram_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET daily_usage = daily_usage + 1 WHERE telegram_id = ?", (telegram_id,))
        await db.commit()

# --- BOT ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class ConverterState(StatesGroup):
    wait_audio = State()
    wait_format = State()

def main_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎵 Konvertatsiya")
    kb.button(text="📊 Statistika")
    kb.button(text="🌟 Obuna olish")
    kb.button(text="📢 Reklama")
    kb.adjust(1, 2)
    return kb.as_markup(resize_keyboard=True)

def format_kb():
    kb = InlineKeyboardBuilder()
    for fmt in TARGET_FORMATS:
        kb.button(text=fmt, callback_data=f"fmt_{fmt}")
    kb.adjust(3)
    return kb.as_markup()

@dp.message(CommandStart())
async def start(message: types.Message):
    await register_user(message.from_user.id)
    await message.answer(f"Salom, {message.from_user.first_name}!\n** ΛTOMIC • Audio Converter ** ga xush kelibsiz. \\\n Foydalanish qoidalari (ToU) bilan tanishing: https://t.me/Atomic_Online_Services/5", reply_markup=main_kb())

@dp.message(F.text == "📊 Statistika")
async def stats(message: types.Message):
    status, usage, max_limit, _ = await check_limits(message.from_user.id)
    max_dur = LIMITS[status]['duration']
    await message.answer(f"👤 **Profil:**\n🏷 Status: **{status.upper()}**\n🔋 Limit: **{usage}/{max_limit}**\n⏱ Maks. uzunlik: **{max_dur}s**")

# --- TO'LOV TIZIMI ---
@dp.message(F.text == "🌟 Obuna olish")
async def buy_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🌟 PLUS (15k)", callback_data="buy_plus")
    kb.button(text="🚀 PRO (30k)", callback_data="buy_pro")
    kb.adjust(1)
    await message.answer("📦 **Tarifni tanlang:**\n\n🌟 **PLUS** (15k/oy)\n• 15 ta fayl\n• 2 daqiqa\n\n🚀 **PRO** (30k/oy)\n• 30 ta fayl\n• 8 daqiqa", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("buy_"))
async def invoice(call: types.CallbackQuery):
    plan = call.data.split("_")[1]
    price = PRICE_PLUS if plan == "plus" else PRICE_PRO
    title = f"{plan.upper()} Obuna"
    await bot.send_invoice(call.message.chat.id, title, "Audio Converter uchun obuna", f"sub_{plan}", PAYMENT_TOKEN, "UZS", [LabeledPrice(label="Obuna", amount=price)])
    await call.answer()

@dp.pre_checkout_query()
async def checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def paid(message: types.Message):
    status = "plus" if "plus" in message.successful_payment.invoice_payload else "pro"
    end = (datetime.now() + timedelta(days=31)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET status = ?, sub_end_date = ? WHERE telegram_id = ?", (status, end, message.from_user.id))
        await db.commit()
    await message.answer(f"✅ To'lov muvaffaqiyatli! Siz endi **{status.upper()}** a'zosisiz.")

# --- KONVERTATSIYA ---
@dp.message(F.text == "🎵 Konvertatsiya")
async def req_audio(message: types.Message, state: FSMContext):
    status, usage, max_limit, is_limited = await check_limits(message.from_user.id)
    if is_limited:
        return await message.answer(f"😔 Limit tugadi ({usage}/{max_limit}). Obuna oling.")
    await message.answer("Audio, Video yoki Ovozli xabar yuboring.")
    await state.set_state(ConverterState.wait_audio)
    
@dp.message(F.text == "📢 Reklama")
async def ads_handler(message: types.Message):
    await message.answer(f"Reklama bo'yicha adminga murojaat qiling: @Al_Abdul_Aziz")

# XATO SHU YERDA EDI, TUZATILDI 👇
@dp.message(ConverterState.wait_audio, F.content_type.in_([ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO, ContentType.DOCUMENT]))
async def get_file(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    now = time.time()
    if uid in THROTTLE_CACHE and (now - THROTTLE_CACHE[uid]) < THROTTLE_LIMIT:
        wait = int(THROTTLE_LIMIT - (now - THROTTLE_CACHE[uid]))
        return await message.answer(f"✋ Shoshmang do'stim, yana {wait} soniya kuting.")
    THROTTLE_CACHE[uid] = now
    
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

    # Fayl turini aniqlash
    if message.audio: fid, ext = message.audio.file_id, os.path.splitext(message.audio.file_name or "a.mp3")[-1]
    elif message.voice: fid, ext = message.voice.file_id, ".ogg"
    elif message.video: fid, ext = message.video.file_id, ".mp4"
    elif message.document: fid, ext = message.document.file_id, os.path.splitext(message.document.file_name or "a.dat")[-1]
    else: return await message.answer("Noto'g'ri fayl.")

    path = os.path.join(DOWNLOAD_DIR, f"{fid}_in{ext}")
    await message.answer("📥 Yuklanmoqda...")
    
    try:
        file = await bot.get_file(fid)
        await bot.download_file(file.file_path, path)
        
        # Uzunlikni tekshirish
        status, _, _, _ = await check_limits(uid)
        dur = len(AudioSegment.from_file(path)) / 1000
        if dur > LIMITS[status]['duration']:
            os.remove(path)
            return await message.answer(f"⚠️ Fayl uzun ({int(dur)}s). Sizning limit: {LIMITS[status]['duration']}s.")
            
    except Exception as e:
        if os.path.exists(path): os.remove(path)
        return await message.answer(f"❌ Xatolik: {e}")

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
        # WAV/FLAC/AIFF = Lossless (PCM), others = compressed
        params = ["-acodec", "pcm_s16le"] if fmt in ["WAV", "FLAC", "AIFF"] else None
        audio.export(out_path, format=ext, parameters=params)
        
        res = FSInputFile(out_path)
        if fmt in ['MP4', 'OGG']: await bot.send_document(call.from_user.id, res, caption=f"✅ {fmt}")
        else: await bot.send_audio(call.from_user.id, res, caption=f"✅ {fmt}")
        
        await update_usage(call.from_user.id)
        os.remove(out_path)
    except:
        await call.message.edit_text("❌ Konvertatsiya xatosi.")
    
    if os.path.exists(in_path): os.remove(in_path)
    await call.message.delete()
    await state.clear()

async def main():
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    await init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
