import os
import logging
import html
import httpx
import sqlite3
import time
from typing import Optional, List, Dict
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
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


class DB:
    def __init__(self, path: str = "bot.db"):
        self.path = path
        self.init()

    def c(self):
        return sqlite3.connect(self.path)

    def init(self):
        with self.c() as con:
            # Настройки пользователя
            con.execute(
                "CREATE TABLE IF NOT EXISTS settings("
                "user_id INTEGER PRIMARY KEY, "
                "max_results INTEGER NOT NULL DEFAULT 5)"
            )

            # История просмотров
            con.execute("""CREATE TABLE IF NOT EXISTS history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ts INTEGER NOT NULL,
                meal_id TEXT NOT NULL,
                meal_name TEXT NOT NULL
            )""")

    def add_history(self, user_id: int, meal_id: str, meal_name: str):
        with self.c() as con:
            con.execute(
                "INSERT INTO history(user_id,ts,meal_id,meal_name) VALUES(?,?,?,?)",
                (user_id, int(time.time()), meal_id, meal_name)
            )

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! I'm a recipes bot 🍽️\nChoose an action:",
        reply_markup=MENU
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n/start - Start bot\n/help - This help\n/random - Random recipe",
        reply_markup=MENU
    )


async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]
    db: DB = context.application.bot_data["db"]

    meal = await api.random()
    if not meal:
        await update.message.reply_text("Nothing found 😕", reply_markup=MENU)
        return

    # Сохраняем в историю
    meal_id = str(meal.get("idMeal") or "")
    meal_name = str(meal.get("strMeal") or "—")
    if meal_id:
        db.add_history(update.effective_user.id, meal_id, meal_name)

    photo = (meal.get("strMealThumb") or "").strip()
    text = meal_full_text(meal)

    if photo:
        await update.message.reply_photo(
            photo=photo,
            caption=f"🍽️ <b>{html.escape(meal.get('strMeal', 'Untitled'))}</b>",
            parse_mode="HTML"
        )

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=MENU)


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

    app.run_polling()


if __name__ == "__main__":
    main()