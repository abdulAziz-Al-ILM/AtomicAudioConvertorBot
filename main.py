import logging
import os
import time
import sys
import asyncio
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command, BaseFilter
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
STICKER_ID = "CAACAgIAAxkBAAIB22kiB7m13F2g7cHuGpIk7iOSuLWcAAJ1jQACbqARSUXlppVMVlpNNgQ"
DOWNLOAD_DIR = "converts"

# --- XAVFSIZLIK SOZLAMALARI (GUARDIAN SYSTEM) ---
FLOOD_LIMIT = 7  # 2 soniya ichida 7 ta xabar yuborsa - bu hujum
FLOOD_WINDOW = 2 # soniya
BANNED_CACHE = set() # Bloklanganlarni xotirada ushlab turish (DB ga har safar kirmaslik uchun)
USER_ACTIVITY = {} # Foydalanuvchi faolligini kuzatish

# --- FORMATLAR ---
TARGET_FORMATS = ["MP3", "WAV", "FLAC", "OGG", "M4A", "AIFF"]
FORMAT_EXTENSIONS = {
    "MP3": "mp3", "OGG": "ogg", "WAV": "wav",
    "FLAC": "flac", "M4A": "mp4", "AIFF": "aiff"
}

# --- LIMITLAR ---
LIMITS = {
    "free": {"daily": 3, "duration": 20},
    "plus": {"daily": 15, "duration": 120},
    "pro": {"daily": 30, "duration": 480}
}
BASE_PRICE_PLUS = 15000 * 100
BASE_PRICE_PRO = 30000 * 100

db_pool = None
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- 🛡️ GUARDIAN SECURITY SYSTEM (MIDDWARE) ---

class SecurityMiddleware(BaseFilter):
    async def __call__(self, message: types.Message) -> bool:
        user_id = message.from_user.id
        
        # 1. Agar Admin bo'lsa, tekshirmaymiz
        if user_id == ADMIN_ID:
            return True

        # 2. BANNED tekshiruvi
        if user_id in BANNED_CACHE:
            return False # Bloklangan foydalanuvchiga javob bermaymiz

        # 3. FLOOD (DDOS) hujumini aniqlash
        now = time.time()
        user_history = USER_ACTIVITY.get(user_id, [])
        
        # Eskirgan yozuvlarni tozalash (oynadan tashqaridagilarni)
        user_history = [t for t in user_history if now - t < FLOOD_WINDOW]
        user_history.append(now)
        USER_ACTIVITY[user_id] = user_history
        
        if len(user_history) > FLOOD_LIMIT:
            # 🚨 HUJUM ANIQLANDI!
            await block_user_attack(user_id, message.from_user.first_name)
            return False
            
        return True

async def block_user_attack(user_id, name):
    if user_id in BANNED_CACHE: return
    
    BANNED_CACHE.add(user_id)
    
    # Adminni ogohlantirish
    alert_msg = (
        f"🛡 **GUARDIAN TIZIMI: Hujum bartaraf etildi!**\n\n"
        f"👤 Hujumchi: {name} (ID: `{user_id}`)\n"
        f"⚔️ Turi: Flood/Spam Attack\n"
        f"🚫 Status: **BLOKLANDI**"
    )
    try:
        await bot.send_message(ADMIN_ID, alert_msg)
        # Hujumchiga oxirgi ogohlantirish
        await bot.send_message(user_id, "⛔️ **Sizning harakatlaringiz bot xavfsizlik tizimi tomonidan bloklandi.**")
    except: pass

# --- DATABASE ---

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
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('discount_percent', '0') ON CONFLICT (key) DO NOTHING"
        )

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

# 🎁 REFERAL BONUS BERISH FUNKSIYASI
async def grant_referral_bonus(referrer_id):
    new_end_date = datetime.now() + timedelta(days=1)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'plus', sub_end_date = $1 WHERE telegram_id = $2 AND status != 'pro'", 
            new_end_date, referrer_id
        )

# 👤 FOYDALANUVCHINI RO'YXATDAN O'TKAZISH (REFERAL QO'LLAB-QUVVATLANADI)
async def register_user(telegram_id, referrer_id=None):
    today = datetime.now().date()
    async with db_pool.acquire() as conn:
        # Foydalanuvchini INSERT qilishga urinish (agar yangi bo'lsa)
        result = await conn.execute(
            "INSERT INTO users (telegram_id, last_usage_date) VALUES ($1, $2) ON CONFLICT (telegram_id) DO NOTHING", 
            telegram_id, today
        )
        # Agar yangi foydalanuvchi muvaffaqiyatli qo'shilgan bo'lsa va referer bo'lsa
        if result == 'INSERT 0 1': 
            if referrer_id and referrer_id != telegram_id:
                referrer = await conn.fetchrow("SELECT telegram_id FROM users WHERE telegram_id = $1", referrer_id)
                if referrer:
                    await conn.execute("UPDATE users SET referrer_id = $1 WHERE telegram_id = $2", referrer_id, telegram_id)
                    await grant_referral_bonus(referrer_id)
                    try:
                        # Refererni ogohlantirish
                        await bot.send_message(referrer_id, "🎁 **Tabriklaymiz!** Referalingiz orqali yangi foydalanuvchi qo'shildi. Sizga **1 kunlik PLUS** obunasi berildi!")
                    except Exception: pass
        
        # Agar foydalanuvchi allaqachon mavjud bo'lsa ham, DB dan ma'lumotni olish kerak
        user = await get_user(telegram_id)
        if not user:
             # Agar yuqorida INSERT bo'lmagan bo'lsa (nadir holat)
             await conn.execute(
                "INSERT INTO users (telegram_id, last_usage_date) VALUES ($1, $2) ON CONFLICT (telegram_id) DO NOTHING", 
                telegram_id, today
            )


async def check_limits(telegram_id):
    today = datetime.now().date()
    user = await get_user(telegram_id)
    if not user:
        # Bu yerda referer_id=None bilan register_user ni chaqiramiz
        await register_user(telegram_id)
        user = await get_user(telegram_id)
        
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
    return status, usage, max_limit, (usage >= max_limit)

async def update_usage(telegram_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET daily_usage = daily_usage + 1 WHERE telegram_id = $1", telegram_id)

def apply_discount(base_price, discount_percent):
    return int(base_price * (1 - discount_percent / 100))

# --- STATES & KEYBOARDS ---
class ConverterState(StatesGroup):
    wait_audio = State()
    wait_format = State()

class AdminState(StatesGroup):
    wait_message = State()
    wait_discount = State()

def main_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎵 Konvertatsiya")
    kb.button(text="📊 Statistika")
    kb.button(text="🌟 Obuna olish")
    kb.button(text="🔗 Referal havolasi")
    kb.button(text="📢 Reklama")
    kb.button(text="ℹ️ Yordam")
    kb.adjust(2, 2, 2)
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
    kb.button(text="🏷 Chegirma o'rnatish") 
    kb.button(text="✉️ Xabar yuborish")
    kb.button(text="❌ Asosiy menyu")
    kb.adjust(1, 2)
    return kb.as_markup(resize_keyboard=True)

# --- HANDLERS ---

# 🛡️ XAVFSIZLIK FILTRINI BARCHA XABARLARGA QO'LLASH
dp.message.filter(SecurityMiddleware())

@dp.message(CommandStart())
async def start(message: types.Message):
    # ⭐️ REFERAL ID NI AJRATIB OLISH MANTIQI
    referrer_id = None
    if message.text and len(message.text.split()) > 1:
        try:
            # /start buyrug'idan ID ni ajratish
            start_param = message.text.split()[1]
            referrer_id = int(start_param)
            # O'zini o'zi chaqirishni taqiqlash
            if referrer_id == message.from_user.id: referrer_id = None 
        except ValueError: referrer_id = None

    await register_user(message.from_user.id, referrer_id)
    await message.answer(f"Assalamu alaykum, {message.from_user.first_name}!\n🔘 [] ΛTOMIC [] taqdim etadi \n🔘 [ ΛTOMIC • Λudio Convertor ] ga xush kelibsiz! \n🌟 Plus va 🚀 Pro obunasi bilan yanada keng imkoniyatga ega bo'ling. \n\n\nFoydalanish qoidalari (ToU) bilan tanishing: https://t.me/Atomic_Online_Services/5", reply_markup=main_kb())

@dp.message(F.text == "📊 Statistika")
async def stats(message: types.Message):
    status, usage, max_limit, _ = await check_limits(message.from_user.id)
    await message.answer(f"👤 Profil:\nStatus: {status.upper()}\nLimit: {usage}/{max_limit}")

@dp.message(F.text == "🔗 Referal havolasi")
async def send_referral_link(message: types.Message):
    bot_info = await bot.get_me()
    # Foydalanuvchi ID si bilan havola yaratish
    link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(
        f"👋 **Do'stlarni taklif qiling!**\n\n"
        f"Agar do'stingiz bu havola orqali botga kirsa, sizga **1 kunlik PLUS** obunasi bonus sifatida beriladi!\n\n"
        f"🔗 **Sizning referal havolangiz:**\n`{link}`", 
        parse_mode="Markdown"
    )

@dp.message(F.text == "🌟 Obuna olish")
async def buy_menu(message: types.Message):
    disc = int(await get_setting('discount_percent') or 0)
    price_plus = apply_discount(BASE_PRICE_PLUS, disc)
    price_pro = apply_discount(BASE_PRICE_PRO, disc)
    
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🌟 PLUS ({price_plus // 100} sum)", callback_data="buy_plus")
    kb.button(text=f"🚀 PRO ({price_pro // 100} sum)", callback_data="buy_pro")
    kb.adjust(1)
    await message.answer(f"Tarifni tanlang. Chegirma: {disc}%", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("buy_"))
async def invoice(call: types.CallbackQuery):
    plan = call.data.split("_")[1]
    disc = int(await get_setting('discount_percent') or 0)
    price = apply_discount(BASE_PRICE_PLUS if plan == "plus" else BASE_PRICE_PRO, disc)
    await bot.send_invoice(call.message.chat.id, f"{plan.upper()} Obuna", "Obuna", f"sub_{plan}", PAYMENT_TOKEN, "UZS", [LabeledPrice(label="Obuna", amount=price)])
    await call.answer()

@dp.pre_checkout_query()
async def checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)
    
@dp.message(F.text == "ℹ️ Yordam")
async def help_msg(message: types.Message):
    await message.answer("Yordam kerakmi? Botdan foydalanish juda oson 😊 \n1. Konvertatsiya tugmasini bosasiz \n2. ovozli xabar, audio fayl yoki video yuborasiz \n3. Konvertatsiya qilinishi kerak bo'lgan formatni tanlaysiz \n4. QArabsizku sizda kerak bo'lgan bo'lgan audio formati tayyor \n🌟 Plus va 🚀 Pro obunasi bilan yanada keng imkoniyatga ega bo'ling. ")
    
@dp.message(F.successful_payment)
async def paid(message: types.Message):
    status = "plus" if "plus" in message.successful_payment.invoice_payload else "pro"
    end = datetime.now() + timedelta(days=31)
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = $1, sub_end_date = $2 WHERE telegram_id = $3", status, end, message.from_user.id)
    await message.answer(f"✅ To'lov muvaffaqiyatli! Status: {status.upper()}")

# --- KONVERTATSIYA (TIMESTAMP NOMI BILAN) ---
@dp.message(F.text == "📢 Reklama")
async def ads_handler(message: types.Message):
    await message.answer(f"Reklama bo'yicha adminga murojaat qiling: @Al_Abdul_Aziz")

@dp.message(F.text == "🎵 Konvertatsiya")
async def req_audio(message: types.Message, state: FSMContext):
    status, usage, max_limit, is_limited = await check_limits(message.from_user.id)
    if is_limited: return await message.answer("😔 Limit tugadi. Obuna oling yoki kimnidir referalingiz orqali taklif qiling. \nTaklif qilsangiz 1 kunlik Plus obunasiga ega bo'lasiz")
    await message.answer("Faylni yuboring (Audio/Video).")
    await state.set_state(ConverterState.wait_audio)

@dp.message(ConverterState.wait_audio, F.content_type.in_([ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO, ContentType.DOCUMENT]))
async def get_file(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

    file_obj = message.audio or message.voice or message.video or message.document
    if not file_obj: return await message.answer("❌ Format noto'g'ri.")
    
    fid = file_obj.file_id
    # Kengaytmani aniqlash
    if message.voice: ext = ".ogg"
    elif message.audio:
        ext = "." + file_obj.mime_type.split('/')[-1] if file_obj.mime_type else ".mp3"
        if file_obj.file_name: ext = os.path.splitext(file_obj.file_name)[-1]
    elif message.video: ext = os.path.splitext(file_obj.file_name or "video.mp4")[-1]
    else: ext = os.path.splitext(file_obj.file_name or "file.dat")[-1]
    
    # Vaqtinchalik kirish fayli
    path_in = os.path.join(DOWNLOAD_DIR, f"{fid}_in{ext}")
    
    try:
        file = await bot.get_file(fid)
        await bot.download_file(file.file_path, path_in)
        
        # Limit tekshiruvi
        status, _, _, _ = await check_limits(uid)
        try:
            dur = len(AudioSegment.from_file(path_in)) / 1000
        except: dur = 0
        
        if dur > LIMITS[status]['duration'] and dur != 0:
            os.remove(path_in)
            return await message.answer(f"⚠️ Limit: {LIMITS[status]['duration']}s. Fayl: {int(dur)}s")

    except Exception as e:
        if os.path.exists(path_in): os.remove(path_in)
        return await message.answer(f"❌ Yuklashda xatolik: {e}")

    await state.update_data(path=path_in)
    await message.answer("Formatni tanlang:", reply_markup=format_kb())
    await state.set_state(ConverterState.wait_format)

@dp.callback_query(ConverterState.wait_format, F.data.startswith("fmt_"))
async def process(call: types.CallbackQuery, state: FSMContext):
    fmt = call.data.split("_")[1]
    ext = FORMAT_EXTENSIONS[fmt]
    data = await state.get_data()
    in_path = data['path']
    
    # 🟢 YANGI: SANA VA VAQT BILAN FAYL NOMI
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = os.path.join(DOWNLOAD_DIR, f"{timestamp}.{ext}")
    
    await call.message.edit_text(f"⏳ {fmt} ga o'girilmoqda...")
    
    try:
        audio = AudioSegment.from_file(in_path)
        params = ["-c:a", "aac", "-b:a", "192k"] if fmt == "M4A" else None
        audio.export(out_path, format=ext, parameters=params)
        
        res = FSInputFile(out_path)
        caption_text = f"✅ {fmt} | 📅 {timestamp}"
        
        if fmt in ['MP3', 'OGG']:
            await bot.send_audio(call.from_user.id, res, caption=caption_text)
        else: 
            await bot.send_document(call.from_user.id, res, caption=caption_text)
        
        await bot.send_document(call.from_user.id, STICKER_ID) 
        await update_usage(call.from_user.id)
        os.remove(out_path)

    except Exception as e:
        await call.message.edit_text(f"❌ Xato: {e}")
        
    if os.path.exists(in_path): os.remove(in_path)
    if os.path.exists(out_path): 
        try: os.remove(out_path)
        except: pass
    await state.clear()

# --- ADMIN ---
@dp.message(Command('admin'))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Admin Panel", reply_markup=admin_kb())

@dp.message(F.text == "📈 Statistika", F.from_user.id == ADMIN_ID)
async def admin_stats(message: types.Message):
    async with db_pool.acquire() as conn:
        cnt = await conn.fetchval("SELECT COUNT(*) FROM users")
    disc = await get_discount()
    revenue = await get_total_revenue()
    await message.answer(f"📊 **Statistika:**\n\n👥 Jami foydalanuvchilar: **{cnt}**\n💰 Jami Daromad: **{revenue:,.0f} UZS**\n🏷 Joriy chegirma: **{disc}%**")

@dp.message(F.text == "🏷 Chegirma o'rnatish", F.from_user.id == ADMIN_ID)
async def admin_disc_ask(message: types.Message, state: FSMContext):
    await message.answer("Chegirma foizini kiriting (0 - 100):", reply_markup=ReplyKeyboardBuilder().button(text="🔙 Chiqish").as_markup(resize_keyboard=True))
    await state.set_state(AdminState.wait_discount)
@dp.message(AdminState.wait_discount, F.from_user.id == ADMIN_ID)
async def admin_disc_set(message: types.Message, state: FSMContext):
    if message.text == "❌ Asosiy menyu":
        await state.clear()
        return await message.answer("Admin panel:", reply_markup=admin_kb())
    if message.text.isdigit():
        perc = int(message.text)
        if 0 <= perc <= 100:
            await set_discount_db(perc)
            await message.answer(f"✅ Chegirma {perc}% etib belgilandi!", reply_markup=admin_kb())
            await state.clear()
        else: await message.answer("0-100 orasi bo'lsin.")
    else: await message.answer("Raqam kiriting.")

@dp.message(F.text == "✉️ Xabar yuborish", F.from_user.id == ADMIN_ID)
async def admin_cast_ask(message: types.Message, state: FSMContext):
    await message.answer("Xabarni kiriting:", reply_markup=ReplyKeyboardBuilder().button(text="🔙 Chiqish").as_markup(resize_keyboard=True))
    await state.set_state(AdminState.wait_broadcast)

@dp.message(AdminState.wait_broadcast, F.from_user.id == ADMIN_ID)
async def admin_cast_send(message: types.Message, state: FSMContext):
    if message.text == "🔙 Chiqish":
        await state.clear()
        return await message.answer("Admin panel:", reply_markup=admin_kb())
    await message.answer("⏳ Yuborilmoqda...")
    count = 0
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT telegram_id FROM users")
        for row in users:
            try:
                # O'zgartirishlar: copy_to bilan xabarni yuborish
                await message.copy_to(row['telegram_id'])
                count += 1
                await asyncio.sleep(0.05)
            except: pass
    await message.answer(f"✅ {count} kishiga yuborildi!", reply_markup=admin_kb())
    await state.clear()

# ... (Qolgan Admin handlerlar avvalgidek) ...

async def main():
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    await init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
