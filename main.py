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
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "CLICK_UZS_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@host/dbname")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
STICKER_ID = "CAACAgIAAxkBAAIB22kiB7m13F2g7cHuGpIk7iOSuLWcAAJ1jQACbqARSUXlppVMVlpNNgQ"
DOWNLOAD_DIR = "converts"

# --- XAVFSIZLIK (GUARDIAN) ---
FLOOD_LIMIT = 7
FLOOD_WINDOW = 2
BANNED_CACHE = set()
USER_ACTIVITY = {}

# --- FORMATLAR VA LIMITLAR ---
TARGET_FORMATS = ["MP3", "WAV", "FLAC", "OGG", "M4A", "AIFF"]
FORMAT_EXTENSIONS = {"MP3": "mp3", "OGG": "ogg", "WAV": "wav", "FLAC": "flac", "M4A": "mp4", "AIFF": "aiff"}
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

# --- GUARDIAN MIDDLEWARE ---
class SecurityMiddleware(BaseFilter):
    async def __call__(self, message: types.Message) -> bool:
        user_id = message.from_user.id
        if user_id == ADMIN_ID:
            return True
        if user_id in BANNED_CACHE:
            return False

        now = time.time()
        history = USER_ACTIVITY.get(user_id, [])
        history = [t for t in history if now - t < FLOOD_WINDOW]
        history.append(now)
        USER_ACTIVITY[user_id] = history

        if len(history) > FLOOD_LIMIT:
            await block_user_attack(user_id, message.from_user.first_name or "NoName")
            return False
        return True

async def block_user_attack(user_id, name):
    if user_id in BANNED_CACHE: return
    BANNED_CACHE.add(user_id)
    alert = (
        f"GUARDIAN: Hujum aniqlandi!\n"
        f"Foydalanuvchi: {name} (`{user_id}`)\n"
        f"Turi: Flood Attack\n"
        f"Status: BLOKLANDI"
    )
    try:
        await bot.send_message(ADMIN_ID, alert, parse_mode="Markdown")
        await bot.send_message(user_id, "Siz bot xavfsizlik tizimi tomonidan bloklandingiz.")
    except: pass

# --- DATABASE ---
async def init_db():
    global db_pool
    logging.info("DB ga ulanmoqda...")
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
            );
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT REFERENCES users(telegram_id),
                amount INTEGER NOT NULL,
                payload TEXT NOT NULL,
                payment_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('discount_percent', '0') ON CONFLICT DO NOTHING"
        )

async def get_setting(key):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        return row['value'] if row else None

async def set_setting(key, value):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT(key) DO UPDATE SET value = $2",
            key, value
        )

async def get_user(telegram_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)

async def register_user(telegram_id, referrer_id=None):
    today = datetime.now().date()
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO users (telegram_id, last_usage_date) VALUES ($1, $2) "
            "ON CONFLICT(telegram_id) DO NOTHING",
            telegram_id, today
        )
        if result == "INSERT 0 1" and referrer_id and referrer_id != telegram_id:
            ref_exists = await conn.fetchrow("SELECT 1 FROM users WHERE telegram_id = $1", referrer_id)
            if ref_exists:
                await conn.execute("UPDATE users SET referrer_id = $1 WHERE telegram_id = $2", referrer_id, telegram_id)
                new_end = datetime.now() + timedelta(days=1)
                await conn.execute(
                    "UPDATE users SET status='plus', sub_end_date=$1 WHERE telegram_id=$2 AND status!='pro'",
                    new_end, referrer_id
                )
                try:
                    await bot.send_message(referrer_id, "Yangi odam keldi! Sizga 1 kunlik PLUS berildi!")
                except: pass

async def check_limits(telegram_id):
    today = datetime.now().date()
    user = await get_user(telegram_id)
    if not user:
        await register_user(telegram_id)
        user = await get_user(telegram_id)

    status = user['status']
    if status in ['plus', 'pro'] and user['sub_end_date'] and datetime.now() > user['sub_end_date']:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET status='free', sub_end_date=NULL WHERE telegram_id=$1", telegram_id)
        status = 'free'

    if user['last_usage_date'] != today:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET daily_usage=0, last_usage_date=$1 WHERE telegram_id=$2", today, telegram_id)

    usage = (await get_user(telegram_id))['daily_usage']
    max_limit = LIMITS[status]['daily']
    return status, usage, max_limit, (usage >= max_limit)

async def update_usage(telegram_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET daily_usage = daily_usage + 1 WHERE telegram_id = $1", telegram_id)

# --- ADMIN FUNKSIYALARI ---
async def get_discount():
    val = await get_setting('discount_percent')
    return int(val) if val else 0

async def get_total_revenue():
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM payments")
        return total // 100

def apply_discount(base, disc):
    return int(base * (1 - disc / 100))

# --- STATES ---
class ConverterState(StatesGroup):
    wait_audio = State()
    wait_format = State()

class AdminState(StatesGroup):
    wait_discount = State()
    wait_broadcast = State()

# --- KEYBOARDS ---
def main_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="Konvertatsiya")
    b.button(text="Statistika")
    b.button(text="Obuna olish")
    b.button(text="Referal havolasi")
    b.button(text="Reklama")
    b.button(text="Yordam")
    b.adjust(2, 2, 2)
    return b.as_markup(resize_keyboard=True)

def format_kb():
    b = InlineKeyboardBuilder()
    for f in TARGET_FORMATS:
        b.button(text=f, callback_data=f"fmt_{f}")
    b.adjust(3)
    return b.as_markup()

def admin_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="Statistika")
    b.button(text="Chegirma o'rnatish")
    b.button(text="Xabar yuborish")
    b.button(text="Asosiy menyu")
    b.adjust(2, 2)
    return b.as_markup(resize_keyboard=True)

# --- HANDLERS ---
dp.message.filter(SecurityMiddleware())

@dp.message(CommandStart())
async def start(message: types.Message):
    ref_id = None
    if len(message.text.split()) > 1:
        try:
            ref_id = int(message.text.split()[1])
            if ref_id == message.from_user.id:
                ref_id = None
        except: pass
    await register_user(message.from_user.id, ref_id)
    await message.answer(
        f"Assalomu alaykum, {message.from_user.first_name}!\n"
        "ΛTOMIC • Audio Convertor ga xush kelibsiz!\n\n"
        "Plus va Pro obuna bilan cheklovlarsiz foydalaning!\n\n"
        "ToS: https://t.me/Atomic_Online_Services/5",
        reply_markup=main_kb()
    )

@dp.message(F.text == "Statistika")
async def stats(message: types.Message):
    status, usage, max_l, _ = await check_limits(message.from_user.id)
    await message.answer(f"Profil:\nStatus: {status.upper()}\nBugun: {usage}/{max_l}")

@dp.message(F.text == "Referal havolasi")
async def referral(message: types.Message):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={message.from_user.id}"
    await message.answer(
        f"Do'stlarni taklif qiling!\nHar bir yangi odam uchun — 1 kun PLUS!\n\n"
        f"Sizning havola:\n`{link}`",
        parse_mode="Markdown"
    )

@dp.message(F.text == "Obuna olish")
async def buy_menu(message: types.Message):
    disc = await get_discount()
    p_plus = apply_discount(BASE_PRICE_PLUS, disc)
    p_pro = apply_discount(BASE_PRICE_PRO, disc)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"PLUS ({p_plus//100} so'm)", callback_data="buy_plus")
    kb.button(text=f"PRO ({p_pro//100} so'm)", callback_data="buy_pro")
    kb.adjust(1)
    await message.answer(f"Tarifni tanlang (Chegirma: {disc}%):", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("buy_"))
async def send_invoice(call: types.CallbackQuery):
    plan = call.data.split("_")[1]
    disc = await get_discount()
    price = apply_discount(BASE_PRICE_PLUS if plan == "plus" else BASE_PRICE_PRO, disc)
    await bot.send_invoice(
        chat_id=call.message.chat.id,
        title=f"{plan.upper()} obuna",
        description="30 kunlik obuna",
        payload=f"sub_{plan}",
        provider_token=PAYMENT_TOKEN,
        currency="UZS",
        prices=[LabeledPrice("Obuna", price)]
    )
    await call.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def paid(message: types.Message):
    payload = message.successful_payment.invoice_payload
    status = "plus" if "plus" in payload else "pro"
    end = datetime.now() + timedelta(days=31)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status=$1, sub_end_date=$2 WHERE telegram_id=$3",
            status, end, message.from_user.id
        )
        await conn.execute(
            "INSERT INTO payments (telegram_id, amount, payload) VALUES ($1, $2, $3)",
            message.from_user.id, message.successful_payment.total_amount, payload
        )
    await message.answer(f"To'lov qabul qilindi! {status.upper()} obuna faollashtirildi.")

@dp.message(F.text == "Yordam")
async def help_cmd(message: types.Message):
    await message.answer("Botdan foydalanish:\n1. «Konvertatsiya»\n2. Audio/Video yuboring\n3. Format tanlang\n4. Tayyor!")

@dp.message(F.text == "Reklama")
async def reklama(message: types.Message):
    await message.answer("Reklama: @Al_Abdul_Aziz")

# --- KONVERTATSIYA ---
@dp.message(F.text == "Konvertatsiya")
async def start_convert(message: types.Message, state: FSMContext):
    status, _, _, limited = await check_limits(message.from_user.id)
    if limited:
        return await message.answer("Bugungi limit tugadi. Obuna oling yoki do'st taklif qiling! \nTaklif qilsangiz 1 kunlik Plus obunasiga ega bo'lasiz")
    await message.answer("Audio yoki video yuboring:")
    await state.set_state(ConverterState.wait_audio)

@dp.message(ConverterState.wait_audio, F.content_type.in_({ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO, ContentType.DOCUMENT}))
async def receive_file(message: types.Message, state: FSMContext):
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    file = message.audio or message.voice or message.video or message.document
    if not file:
        return await message.answer("Fayl topilmadi.")

    file_id = file.file_id
    ext = ".ogg"
    if message.audio and file.file_name:
        ext = os.path.splitext(file.file_name)[1] or ".mp3"
    elif message.video and file.file_name:
        ext = os.path.splitext(file.file_name)[1]

    in_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_in{ext}")
    await bot.download_file_by_id(file_id, in_path)

    # Davomiylik tekshiruvi
    status, _, _, _ = await check_limits(message.from_user.id)
    try:
        duration = len(AudioSegment.from_file(in_path)) / 1000
        if duration > LIMITS[status]['duration']:
            os.remove(in_path)
            return await message.answer(f"Fayl juda uzun. Limit: {LIMITS[status]['duration']}s")
    except: pass

    await state.update_data(path=in_path)
    await message.answer("Formatni tanlang:", reply_markup=format_kb())
    await state.set_state(ConverterState.wait_format)

@dp.callback_query(ConverterState.wait_format, F.data.startswith("fmt_"))
async def convert(call: types.CallbackQuery, state: FSMContext):
    fmt = call.data.split("_")[1].upper()
    ext = FORMAT_EXTENSIONS[fmt]
    data = await state.get_data()
    in_path = data['path']

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = os.path.join(DOWNLOAD_DIR, f"{timestamp}.{ext}")

    await call.message.edit_text(f"{fmt} ga aylantirilmoqda...")

    try:
        audio = AudioSegment.from_file(in_path)
        params = ["-c:a", "aac", "-b:a", "192k"] if fmt == "M4A" else None
        audio.export(out_path, format=ext.lower(), parameters=params)

        doc = FSInputFile(out_path)
        caption = f"{fmt} | {timestamp}"

        if fmt in ["MP3", "OGG"]:
            await bot.send_audio(call.from_user.id, doc, caption=caption)
        else:
            await bot.send_document(call.from_user.id, doc, caption=caption)

        await bot.send_sticker(call.from_user.id, STICKER_ID)
        await update_usage(call.from_user.id)
    except Exception as e:
        await call.message.edit_text(f"Xato: {e}")
    finally:
        for p in [in_path, out_path]:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass
    await state.clear()

# --- ADMIN PANEL ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Admin panel", reply_markup=admin_kb())

@dp.message(F.text == "Statistika")
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        paid = await conn.fetchval("SELECT COUNT(*) FROM users WHERE status IN ('plus','pro')")
    revenue = await get_total_revenue()
    disc = await get_discount()
    await message.answer(
        f"Statistika\n\n"
        f"Jami odam: {total_users}\n"
        f"Pullik: {paid}\n"
        f"Daromad: {revenue:,} UZS\n"
        f"Chegirma: {disc}%",
        reply_markup=admin_kb()
    )

@dp.message(F.text == "Chegirma o'rnatish")
async def set_discount_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    kb = ReplyKeyboardBuilder().button(text="Ortga").as_markup(resize_keyboard=True)
    await message.answer(f"Joriy chegirma: {await get_discount()}%\nYangi foiz (0-100):", reply_markup=kb)
    await state.set_state(AdminState.wait_discount)

@dp.message(AdminState.wait_discount)
async def set_discount_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if message.text == "Ortga":
        await state.clear()
        return await message.answer("Admin panel", reply_markup=admin_kb())
    if not message.text.isdigit() or not (0 <= int(message.text) <= 100):
        return await message.answer("0-100 orasida raqam kiriting!")
    await set_setting("discount_percent", message.text)
    await message.answer(f"Chegirma {message.text}% qilib o'rnatildi!", reply_markup=admin_kb())
    await state.clear()

@dp.message(F.text == "Xabar yuborish")
async def broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    kb = ReplyKeyboardBuilder().button(text="Ortga").as_markup(resize_keyboard=True)
    await message.answer("Hamma foydalanuvchilarga yuboriladigan xabarni yuboring:", reply_markup=kb)
    await state.set_state(AdminState.wait_broadcast)

@dp.message(AdminState.wait_broadcast)
async def broadcast_send(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    if message.text == "Ortga":
        await state.clear()
        return await message.answer("Admin panel", reply_markup=admin_kb())

    await message.answer("Yuborilmoqda...")
    success = failed = 0
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT telegram_id FROM users")
    for u in users:
        try:
            await message.copy_to(u['telegram_id'])
            success += 1
            await asyncio.sleep(0.04)
        except:
            failed += 1
    await message.answer(f"Yuborildi: {success}\nXato: {failed}", reply_markup=admin_kb())
    await state.clear()

@dp.message(F.text.in_({"Asosiy menyu", "Ortga"}))
async def back_admin(message: types.Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await state.clear()
        await message.answer("Admin panel", reply_markup=admin_kb())
    else:
        await state.clear()
        await message.answer("Bosh sahifa", reply_markup=main_kb())

# --- START ---
async def main():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)
    await init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
