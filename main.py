import asyncio
import logging
import os
import random
import sqlite3
import time
from typing import List, Dict, Any

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# === CONFIG ===
TOKEN = os.environ.get("TG_BOT_TOKEN", "REPLACE_WITH_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # set your telegram id for admin features
DB_PATH = os.environ.get("DB_PATH", "/mnt/data/case_bot.db")

# === SETUP ===
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# === DB HELPERS ===
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    # users: id, stars balance, ton balance, ref_by
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        stars INTEGER DEFAULT 100,
        ton INTEGER DEFAULT 0,
        ref_by INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer INTEGER,
        referee INTEGER,
        created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        rarity TEXT,
        sell_price INTEGER,
        is_telegram_gift INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        price INTEGER
    );
    CREATE TABLE IF NOT EXISTS case_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER,
        item_id INTEGER,
        weight INTEGER
    );
    CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        item_id INTEGER,
        created_at INTEGER,
        UNIQUE(user_id, item_id, created_at)
    );
    CREATE TABLE IF NOT EXISTS codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        item_id INTEGER,
        used INTEGER DEFAULT 0
    );
    """)
    conn.commit()
    # seed simple items and a case if empty
    cur.execute("SELECT COUNT(*) as c FROM items")
    if cur.fetchone()["c"] == 0:
        items = [
            ("Cookie (common)", "common", 10, 0),
            ("Sticker pack (rare)", "rare", 50, 0),
            ("Telegram Gift (special)", "special", 0, 1),
            ("Golden Token (epic)", "epic", 150, 0),
        ]
        cur.executemany("INSERT INTO items (name, rarity, sell_price, is_telegram_gift) VALUES (?, ?, ?, ?)", items)
        conn.commit()
    cur.execute("SELECT COUNT(*) as c FROM cases")
    if cur.fetchone()["c"] == 0:
        cur.execute("INSERT INTO cases (name, price) VALUES (?,?)", ("Free Box", 0))
        case_id = cur.lastrowid
        # attach items with weights
        cur.execute("SELECT id FROM items")
        item_ids = [row["id"] for row in cur.fetchall()]
        weights = [70, 20, 5, 5]
        for iid, w in zip(item_ids, weights):
            cur.execute("INSERT INTO case_items (case_id, item_id, weight) VALUES (?,?,?)", (case_id, iid, w))
        conn.commit()
    conn.close()

# === UTIL ===
def ensure_user(user_id: int, ref_by: int = 0):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO users (user_id, stars, ton, ref_by) VALUES (?,?,?,?)", (user_id, 100, 0, ref_by))
        conn.commit()
        if ref_by and ref_by != user_id:
            cur.execute("INSERT INTO referrals (referrer, referee, created_at) VALUES (?,?,?)", (ref_by, user_id, int(time.time())))
            # reward both
            cur.execute("UPDATE users SET stars = stars + 20 WHERE user_id = ?", (ref_by,))
            cur.execute("UPDATE users SET stars = stars + 10 WHERE user_id = ?", (user_id,))
            conn.commit()
    conn.close()

def get_balance(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT stars, ton FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return row["stars"], row["ton"]
    return 0,0

def add_stars(user_id: int, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET stars = stars + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def add_ton(user_id: int, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET ton = ton + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def list_cases():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cases")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_case(case_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cases WHERE id = ?", (case_id,))
    row = cur.fetchone()
    conn.close()
    return row

def open_case_and_get_item(case_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT ci.item_id, ci.weight, i.name, i.is_telegram_gift FROM case_items ci JOIN items i ON i.id = ci.item_id WHERE ci.case_id = ?", (case_id,))
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return None
    weights = [r["weight"] for r in rows]
    choices = [r["item_id"] for r in rows]
    picked = random.choices(rows, weights=weights, k=1)[0]
    # add to inventory
    cur.execute("INSERT INTO inventory (user_id, item_id, created_at) VALUES (?,?,?)", (CURRENT_USER_ID_FOR_DB, picked["item_id"], int(time.time())))
    conn.commit()
    # if item is telegram gift -> create a redeem code (simulate)
    code = None
    if picked["is_telegram_gift"]:
        code = f"TG-GIFT-{int(time.time())}-{random.randint(1000,9999)}"
        cur.execute("INSERT INTO codes (code, item_id) VALUES (?,?)", (code, picked["item_id"]))
        conn.commit()
    conn.close()
    return {"item_id": picked["item_id"], "name": picked["name"], "code": code}

def get_inventory(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT inv.id as inv_id, i.* , inv.created_at FROM inventory inv JOIN items i ON i.id = inv.item_id WHERE inv.user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def sell_item(inv_id: int, user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT item_id FROM inventory WHERE id = ? AND user_id = ?", (inv_id, user_id))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "Item not found"
    item_id = row["item_id"]
    cur.execute("SELECT sell_price FROM items WHERE id = ?", (item_id,))
    ip = cur.fetchone()
    price = ip["sell_price"] if ip else 0
    cur.execute("DELETE FROM inventory WHERE id = ?", (inv_id,))
    cur.execute("UPDATE users SET ton = ton + ? WHERE user_id = ?", (price, user_id))
    conn.commit()
    conn.close()
    return True, price

# === GLOBAL STATE NOTE ===
# Because open_case_and_get_item needs current user id for DB insertion, we use a simple thread-local approach.
# In production, refactor to pass user_id explicitly.
CURRENT_USER_ID_FOR_DB = None

# === HANDLERS ===

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    global CURRENT_USER_ID_FOR_DB
    payload = message.get_args()
    ref_by = 0
    if payload and payload.isdigit():
        ref_by = int(payload)
    ensure_user(message.from_user.id, ref_by)
    await message.answer(f"Привет, {message.from_user.first_name}!\nВы получили начальные 100 ⭐.\nВаш реф: {message.from_user.id}\nИспользуйте /shop для кейсов, /inventory для вещей, /balance для баланса.")

@dp.message(Command(commands=["balance"]))
async def cmd_balance(message: types.Message):
    stars, ton = get_balance(message.from_user.id)
    await message.reply(f"Баланс:\n⭐ Stars: {stars}\nTON (internal): {ton}")

@dp.message(Command(commands=["shop","cases"]))
async def cmd_shop(message: types.Message):
    cases = list_cases()
    kb = InlineKeyboardBuilder()
    for c in cases:
        kb.button(text=f"{c['name']} — {c['price']} ⭐", callback_data=f"buycase:{c['id']}")
    kb.adjust(1)
    await message.answer("Магазин кейсов:", reply_markup=kb.as_markup())

@dp.callback_query(lambda c: c.data and c.data.startswith("buycase:"))
async def cb_buycase(call: types.CallbackQuery):
    case_id = int(call.data.split(":")[1])
    case = get_case(case_id)
    if not case:
        await call.message.edit_text("Кейс не найден.")
        return
    stars, _ = get_balance(call.from_user.id)
    if stars < case["price"]:
        await call.answer("Недостаточно звезд.", show_alert=True)
        return
    # pay and open
    add_stars(call.from_user.id, -case["price"])
    global CURRENT_USER_ID_FOR_DB
    CURRENT_USER_ID_FOR_DB = call.from_user.id
    result = open_case_and_get_item(case_id)
    CURRENT_USER_ID_FOR_DB = None
    if not result:
        await call.message.edit_text("Ошибка открытия кейса.")
        return
    text = f"Вы открыли {case['name']} и получили: {result['name']}!"
    if result.get("code"):
        text += f"\n🎁 Telegram gift: код для получения — {result['code']}"
    await call.message.answer(text)
    await call.answer()

@dp.message(Command(commands=["inventory"]))
async def cmd_inventory(message: types.Message):
    inv = get_inventory(message.from_user.id)
    if not inv:
        await message.reply("Инвентарь пуст.")
        return
    text_lines = []
    for row in inv:
        text_lines.append(f"InvID:{row['inv_id']} — {row['name']} ({row['rarity']}) — sell {row['sell_price']} TON")
    text = "\n".join(text_lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Продать первый", callback_data=f"sell_first")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("sell_first"))
async def cb_sell_first(call: types.CallbackQuery):
    inv = get_inventory(call.from_user.id)
    if not inv:
        await call.answer("Инвентарь пуст.", show_alert=True)
        return
    inv_id = inv[0]["inv_id"]
    ok, price = sell_item(inv_id, call.from_user.id)
    if ok:
        await call.message.answer(f"Вы продали предмет за {price} TON.")
    else:
        await call.answer(price, show_alert=True)

# === Roulette animation ===
WHEEL = ["🔴","🟠","🟡","🟢","🔵","🟣","⚫️","⚪️"]
@dp.message(Command(commands=["roulette"]))
async def cmd_roulette(message: types.Message):
    parts = message.get_args().split()
    if not parts or not parts[0].isdigit():
        await message.reply("Использование: /roulette <ставка (stars)>")
        return
    bet = int(parts[0])
    stars, _ = get_balance(message.from_user.id)
    if bet <= 0 or bet > stars:
        await message.reply("Неверная ставка.")
        return
    add_stars(message.from_user.id, -bet)
    # start animation
    msg = await message.reply("🎰 Запуск рулетки...")
    pos = random.randrange(len(WHEEL))
    rounds = random.randint(10, 20)
    for i in range(rounds):
        pos = (pos + 1) % len(WHEEL)
        wheel_view = " ".join(WHEEL[(pos + j) % len(WHEEL)] for j in range(len(WHEEL)))
        try:
            await msg.edit_text(f"🎰 {wheel_view}")
        except TelegramBadRequest:
            pass
        await asyncio.sleep(0.15 + i*0.01)
    # determine outcome: if landing on green (🟢) user wins 3x
    symbol = WHEEL[pos]
    if symbol == "🟢":
        win = bet * 3
        add_stars(message.from_user.id, win)
        await msg.edit_text(f"🎉 Победа! Выпало {symbol}. Вы выиграли {win} ⭐.")
    else:
        await msg.edit_text(f"💥 Проигрыш. Выпало {symbol}. Вы потеряли {bet} ⭐.")

# === Admin commands ===
@dp.message(Command(commands=["admin"]))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("Доступно только админ.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Give stars", callback_data="admin_give_stars")],
        [InlineKeyboardButton("List users", callback_data="admin_list_users")],
    ])
    await message.answer("Панель администратора:", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("admin_"))
async def cb_admin(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    if call.data == "admin_give_stars":
        await call.message.answer("Используйте: /give <user_id> <amount>")
    elif call.data == "admin_list_users":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT user_id, stars, ton FROM users ORDER BY stars DESC LIMIT 50")
        rows = cur.fetchall()
        conn.close()
        text = "Пользователи:\n" + "\n".join(f"{r['user_id']} — {r['stars']} ⭐ / {r['ton']} TON" for r in rows)
        await call.message.answer(text)
    await call.answer()

@dp.message(Command(commands=["give"]))
async def cmd_give(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("Нет доступа.")
        return
    parts = message.get_args().split()
    if len(parts) < 2:
        await message.reply("Использование: /give <user_id> <stars>")
        return
    uid = int(parts[0]); amt = int(parts[1])
    add_stars(uid, amt)
    await message.reply(f"Дано {amt} ⭐ пользователю {uid}.")

# === Startup ===
async def main():
    init_db()
    print("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
