import os
import logging
import html
import httpx
import sqlite3
import time
from typing import Optional, List, Dict, Tuple
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("recipe_bot")

# UI константы
BTN_ING = "🔎 By ingredients"
BTN_NAME = "🍲 By name"
BTN_AREA = "🌍 By cuisine"
BTN_CAT = "🏷️ By category"
BTN_RANDOM = "🎲 Random"
BTN_HISTORY = "🕘 History"
BTN_FAVS = "⭐ Favorites"
BTN_SETTINGS = "⚙️ Settings"
BTN_HELP = "ℹ️ Help"
BTN_BACK = "⬅️ Back"

MENU = ReplyKeyboardMarkup(
    [
        [BTN_ING, BTN_NAME],
        [BTN_AREA, BTN_CAT],
        [BTN_RANDOM, BTN_HISTORY],
        [BTN_FAVS, BTN_SETTINGS],
        [BTN_HELP],
    ],
    resize_keyboard=True,
)
BACK = ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True)


# ---------- DB ----------
class DB:
    def __init__(self, path: str = "bot.db"):
        self.path = path
        self.init()

    def c(self):
        return sqlite3.connect(self.path)

    def init(self):
        with self.c() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS settings("
                "user_id INTEGER PRIMARY KEY, "
                "max_results INTEGER NOT NULL DEFAULT 5)"
            )

            con.execute("""CREATE TABLE IF NOT EXISTS history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ts INTEGER NOT NULL,
                meal_id TEXT NOT NULL,
                meal_name TEXT NOT NULL
            )""")

            con.execute("""CREATE TABLE IF NOT EXISTS favorites(
                user_id INTEGER NOT NULL,
                meal_id TEXT NOT NULL,
                meal_name TEXT NOT NULL,
                ts INTEGER NOT NULL,
                PRIMARY KEY(user_id, meal_id)
            )""")

    def add_history(self, user_id: int, meal_id: str, meal_name: str):
        with self.c() as con:
            con.execute(
                "INSERT INTO history(user_id,ts,meal_id,meal_name) VALUES(?,?,?,?)",
                (user_id, int(time.time()), meal_id, meal_name)
            )

    def get_max(self, user_id: int) -> int:
        with self.c() as con:
            row = con.execute(
                "SELECT max_results FROM settings WHERE user_id=?",
                (user_id,)
            ).fetchone()
            if not row:
                con.execute(
                    "INSERT INTO settings(user_id,max_results) VALUES(?,5)",
                    (user_id,)
                )
                return 5
            return int(row[0])

    def is_fav(self, user_id: int, meal_id: str) -> bool:
        with self.c() as con:
            row = con.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND meal_id=?",
                (user_id, meal_id)
            ).fetchone()
        return bool(row)

    def add_fav(self, user_id: int, meal_id: str, meal_name: str):
        with self.c() as con:
            con.execute(
                "INSERT OR REPLACE INTO favorites(user_id, meal_id, meal_name, ts) VALUES(?,?,?,?)",
                (user_id, meal_id, meal_name, int(time.time()))
            )

    def del_fav(self, user_id: int, meal_id: str):
        with self.c() as con:
            con.execute("DELETE FROM favorites WHERE user_id=? AND meal_id=?", (user_id, meal_id))


# ---------- TheMealDB API ----------
class MealDB:
    def __init__(self, api_key: str = "1"):
        self.base = f"https://www.themealdb.com/api/json/v1/{api_key}"
        self.timeout = httpx.Timeout(12.0, connect=6.0)

    async def get(self, path: str, params: dict):
        url = f"{self.base}/{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(url, params=params)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            log.error(f"API error: {e}")
            return {"meals": None}

    async def random(self) -> Optional[dict]:
        d = await self.get("random.php", {})
        m = d.get("meals") or []
        return m[0] if m else None

    async def search_name(self, q: str) -> List[dict]:
        d = await self.get("search.php", {"s": q})
        return d.get("meals") or []

    async def lookup(self, meal_id: str) -> Optional[dict]:
        d = await self.get("lookup.php", {"i": meal_id})
        m = d.get("meals") or []
        return m[0] if m else None

    async def filter_ing(self, ing: str) -> List[dict]:
        d = await self.get("filter.php", {"i": ing})
        return d.get("meals") or []


def parse_ingredients(s: str) -> List[str]:
    s = (s or "").replace(";", ",").replace("\n", ",")
    items = [x.strip() for x in s.split(",") if x.strip()]
    return [x.lower().replace(" ", "_") for x in items][:8]


def meal_full_text(meal: dict) -> str:
    name = (meal.get("strMeal") or "Untitled").strip()
    cat = (meal.get("strCategory") or "—").strip()
    area = (meal.get("strArea") or "—").strip()
    instr = (meal.get("strInstructions") or "No instructions provided.").strip()

    ingredients = []
    for i in range(1, 21):
        ing = (meal.get(f"strIngredient{i}") or "").strip()
        meas = (meal.get(f"strMeasure{i}") or "").strip()
        if ing:
            ingredients.append(f"• {ing}" + (f" — {meas}" if meas else ""))

    ings_text = "\n".join(ingredients) if ingredients else "—"

    body = (
        f"🍽️ <b>{html.escape(name)}</b>\n"
        f"🏷️ {html.escape(cat)} • {html.escape(area)}\n\n"
        f"<b>Ingredients:</b>\n{html.escape(ings_text)}\n\n"
        f"<b>Instructions:</b>\n{html.escape(instr)}"
    )
    return body[:3800]


def fav_kb(db: DB, user_id: int, meal_id: str) -> InlineKeyboardMarkup:
    is_f = db.is_fav(user_id, meal_id)
    label = "✅ In favorites" if is_f else "⭐ Add to favorites"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"fav:{meal_id}")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
    ])


async def send_meal(msg, context: ContextTypes.DEFAULT_TYPE, meal: dict, user_id: int):
    db: DB = context.application.bot_data["db"]

    meal_id = str(meal.get("idMeal") or "")
    meal_name = str(meal.get("strMeal") or "—")

    if meal_id:
        db.add_history(user_id, meal_id, meal_name)

    photo = (meal.get("strMealThumb") or "").strip()
    text = meal_full_text(meal)
    kb = fav_kb(db, user_id, meal_id) if meal_id else None

    if photo:
        await msg.reply_photo(
            photo=photo,
            caption=f"🍽️ <b>{html.escape(meal.get('strMeal', 'Untitled'))}</b>",
            parse_mode="HTML"
        )

    await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mode", None)
    await update.message.reply_text(
        "Hi! I'm a recipes bot 🍽️\nChoose an action:",
        reply_markup=MENU
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n/start - Start\n/help - Help\n/random - Random recipe\n/name - Search by name\n/find - Search by ingredients",
        reply_markup=MENU
    )


async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]
    meal = await api.random()

    if not meal:
        await update.message.reply_text("Nothing found 😕", reply_markup=MENU)
        return

    await send_meal(update.message, context, meal, update.effective_user.id)


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "name"
    await update.message.reply_text(
        "Send a recipe name (English):",
        reply_markup=BACK
    )


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "ing"
    await update.message.reply_text(
        "Send ingredients separated by commas (English):",
        reply_markup=BACK
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]
    db: DB = context.application.bot_data["db"]

    text = (update.message.text or "").strip()

    # Кнопка Назад
    if text == BTN_BACK:
        context.user_data.pop("mode", None)
        await update.message.reply_text("OK.", reply_markup=MENU)
        return

    # Кнопки меню
    if text == BTN_HELP:
        await help_cmd(update, context)
        return
    if text == BTN_RANDOM:
        await random_cmd(update, context)
        return
    if text == BTN_NAME:
        await name_cmd(update, context)
        return
    if text == BTN_ING:
        await find_cmd(update, context)
        return

    mode = context.user_data.get("mode")
    limit = min(max(db.get_max(update.effective_user.id), 1), 10)

    if mode == "name":
        meals = await api.search_name(text)
        context.user_data.pop("mode", None)

        if not meals:
            await update.message.reply_text("No results 😕", reply_markup=MENU)
            return

        kb_rows = []
        for meal in meals[:limit]:
            name = meal.get("strMeal", "—")
            meal_id = meal.get("idMeal", "")
            kb_rows.append([InlineKeyboardButton(name, callback_data=f"meal:{meal_id}")])
        kb_rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])

        await update.message.reply_text(
            "Choose a recipe:",
            reply_markup=InlineKeyboardMarkup(kb_rows)
        )
        return

    if mode == "ing":
        ings = parse_ingredients(text)
        if not ings:
            await update.message.reply_text("Example: chicken, garlic", reply_markup=BACK)
            return

        sets = []
        name_by = {}
        for ing in ings:
            items = await api.filter_ing(ing)
            ids = set()
            for it in items:
                mid = it.get("idMeal")
                if mid:
                    ids.add(mid)
                    name_by[mid] = it.get("strMeal", "—")
            sets.append(ids)

        common = set.intersection(*sets) if sets else set()
        context.user_data.pop("mode", None)

        if not common:
            await update.message.reply_text("No matches 😕", reply_markup=MENU)
            return

        kb_rows = []
        for mid in list(common)[:limit]:
            name = name_by.get(mid, "—")
            kb_rows.append([InlineKeyboardButton(name, callback_data=f"meal:{mid}")])
        kb_rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])

        await update.message.reply_text(
            "Choose a recipe:",
            reply_markup=InlineKeyboardMarkup(kb_rows)
        )
        return

    await update.message.reply_text("Use the menu buttons 🙂", reply_markup=MENU)


async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]
    db: DB = context.application.bot_data["db"]
    query = update.callback_query
    data = query.data

    await query.answer()

    if data == "menu":
        await query.message.reply_text("Menu:", reply_markup=MENU)
        return

    if data.startswith("meal:"):
        meal_id = data.split(":", 1)[1]
        meal = await api.lookup(meal_id)

        if not meal:
            await query.message.reply_text("Failed to load 😕", reply_markup=MENU)
            return

        await send_meal(query.message, context, meal, query.from_user.id)
        return

    if data.startswith("fav:"):
        meal_id = data.split(":", 1)[1]

        if db.is_fav(query.from_user.id, meal_id):
            db.del_fav(query.from_user.id, meal_id)
        else:
            meal = await api.lookup(meal_id)
            title = meal.get("strMeal", "—") if meal else "—"
            db.add_fav(query.from_user.id, meal_id, title)

        # Обновляем кнопку
        try:
            await query.message.edit_reply_markup(
                reply_markup=fav_kb(db, query.from_user.id, meal_id)
            )
        except:
            pass
        return


def main():
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing TELEGRAM_TOKEN in .env")

    api_key = os.getenv("MEALDB_API_KEY", "1").strip() or "1"
    db_path = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"

    app = Application.builder().token(token).build()
    app.bot_data["api"] = MealDB(api_key)
    app.bot_data["db"] = DB(db_path)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("random", random_cmd))
    app.add_handler(CommandHandler("name", name_cmd))
    app.add_handler(CommandHandler("find", find_cmd))

    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()


if __name__ == "__main__":
    main()