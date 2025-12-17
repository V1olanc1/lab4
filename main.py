import os
import logging
import html
import httpx
import sqlite3
import time
import traceback
from typing import Optional, List, Dict, Tuple
from urllib.parse import quote, unquote
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

PAGE_SIZE = 20


class UserError(Exception):
    """Expected errors shown to user nicely."""
    pass


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
            # Keep only last 200 entries per user
            con.execute(
                "DELETE FROM history WHERE user_id=? AND id NOT IN "
                "(SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT 200)",
                (user_id, user_id)
            )

    def get_history(self, user_id: int, limit: int):
        with self.c() as con:
            rows = con.execute(
                "SELECT meal_id, meal_name, ts FROM history "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
        return [(str(a), str(b), int(c)) for a, b, c in rows]

    def clear_history(self, user_id: int):
        with self.c() as con:
            con.execute("DELETE FROM history WHERE user_id=?", (user_id,))

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

    def clear_favs(self, user_id: int):
        with self.c() as con:
            con.execute("DELETE FROM favorites WHERE user_id=?", (user_id,))

    def get_favs(self, user_id: int, limit: int):
        with self.c() as con:
            rows = con.execute(
                "SELECT meal_id, meal_name, ts FROM favorites "
                "WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (user_id, limit)
            ).fetchall()
        return [(str(a), str(b), int(c)) for a, b, c in rows]

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

    def set_max(self, user_id: int, val: int):
        with self.c() as con:
            con.execute(
                "INSERT INTO settings(user_id,max_results) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET max_results=excluded.max_results",
                (user_id, val)
            )


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
        except httpx.TimeoutException:
            raise UserError("⏱️ API timeout. Please try again.")
        except httpx.RequestError:
            raise UserError("🌐 Network error. Please try again.")
        except httpx.HTTPStatusError:
            raise UserError("🌐 API error. Please try later.")
        except ValueError:
            raise UserError("⚠️ Invalid API response.")

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

    async def list_areas(self) -> List[str]:
        d = await self.get("list.php", {"a": "list"})
        meals = d.get("meals") or []
        out = []
        for x in meals:
            a = (x.get("strArea") or "").strip()
            if a:
                out.append(a)
        return sorted(set(out))

    async def list_categories(self) -> List[str]:
        d = await self.get("list.php", {"c": "list"})
        meals = d.get("meals") or []
        out = []
        for x in meals:
            c = (x.get("strCategory") or "").strip()
            if c:
                out.append(c)
        return sorted(set(out))

    async def filter_area(self, area: str) -> List[dict]:
        d = await self.get("filter.php", {"a": area})
        return d.get("meals") or []

    async def filter_category(self, category: str) -> List[dict]:
        d = await self.get("filter.php", {"c": category})
        return d.get("meals") or []


def parse_ingredients(s: str) -> List[str]:
    s = (s or "").replace(";", ",").replace("\n", ",")
    items = [x.strip() for x in s.split(",") if x.strip()]
    return [x.lower().replace(" ", "_") for x in items][:8]


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


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
    return trunc(body, 3800)


def clamp(n: int, lo: int, hi: int) -> int:
    return lo if n < lo else hi if n > hi else n


def paginate(items: List, page: int, page_size: int) -> Tuple[List, int]:
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = clamp(page, 0, total_pages - 1)
    start_idx = page * page_size
    end_idx = start_idx + page_size
    return items[start_idx:end_idx], total_pages


def list_kb(items: List[str], prefix: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for x in items:
        row.append(InlineKeyboardButton(x, callback_data=f"{prefix}:sel:{quote(x)}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:page:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def meals_kb(meals: List[dict], kind: str, value: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    for m in meals:
        mid = str(m.get("idMeal") or "")
        name = str(m.get("strMeal") or "—")
        rows.append([InlineKeyboardButton(name, callback_data=f"meal:{mid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{kind}_meals:page:{quote(value)}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{kind}_meals:page:{quote(value)}:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"{kind}:page:0")])
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def fav_kb(db: DB, user_id: int, meal_id: str) -> InlineKeyboardMarkup:
    is_f = db.is_fav(user_id, meal_id)
    label = "✅ In favorites" if is_f else "⭐ Add to favorites"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"fav:{meal_id}")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
    ])


def fav_list_kb(items: List[Tuple[str, str, int]]) -> InlineKeyboardMarkup:
    rows = []
    for mid, name, _ in items:
        rows.append([InlineKeyboardButton(name, callback_data=f"meal:{mid}")])
        rows.append([InlineKeyboardButton("🗑 Remove", callback_data=f"unfav:{mid}")])
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def confirm_kb(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"confirm:{kind}:yes"),
            InlineKeyboardButton("❌ No", callback_data=f"confirm:{kind}:no"),
        ],
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


async def safe_run(update: Update, context: ContextTypes.DEFAULT_TYPE, coro) -> None:
    try:
        await coro
        return
    except UserError as e:
        if update.effective_message:
            await update.effective_message.reply_text(str(e), reply_markup=MENU)
        return
    except Exception as e:
        log.error("Unexpected error: %s", e)
        log.debug("Traceback:\n%s", traceback.format_exc())
        if update.effective_message:
            await update.effective_message.reply_text("⚠️ Oops, something went wrong. Please try again.",
                                                      reply_markup=MENU)
        return


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mode", None)
    await update.message.reply_text(
        "Hi! I'm a recipes bot 🍽️\nChoose an action:",
        reply_markup=MENU
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n/start - Start\n/help - Help\n/random - Random recipe\n"
        "/name - Search by name\n/find - Search by ingredients\n"
        "/cuisines - Browse cuisines\n/categories - Browse categories\n"
        "/history - History\n/favorites - Favorites\n"
        "/clearhistory - Clear history\n/clearfavorites - Clear favorites\n"
        "/settings - Settings",
        reply_markup=MENU
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: DB = context.application.bot_data["db"]
    m = db.get_max(update.effective_user.id)
    context.user_data["mode"] = "set_max"
    await update.message.reply_text(
        f"⚙️ Current max results = {m}\nSend a number 1–10:",
        reply_markup=BACK
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: DB = context.application.bot_data["db"]
    limit = clamp(db.get_max(update.effective_user.id), 1, 10)
    items = db.get_history(update.effective_user.id, limit)

    if not items:
        await update.message.reply_text("History is empty 🙂", reply_markup=MENU)
        return

    kb = [[InlineKeyboardButton(name, callback_data=f"meal:{mid}")] for mid, name, _ in items]
    kb.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])

    await update.message.reply_text(
        "🕘 Recent views:",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def favorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: DB = context.application.bot_data["db"]
    limit = clamp(db.get_max(update.effective_user.id), 1, 10)
    items = db.get_favs(update.effective_user.id, limit)

    if not items:
        await update.message.reply_text("Favorites is empty 🙂", reply_markup=MENU)
        return

    await update.message.reply_text(
        "⭐ Favorites:",
        reply_markup=fav_list_kb(items)
    )


async def clearhistory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Clear history?",
        reply_markup=confirm_kb("history")
    )


async def clearfavorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Clear favorites?",
        reply_markup=confirm_kb("favorites")
    )


async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]

    async def _do():
        meal = await api.random()
        if not meal:
            await update.message.reply_text("Nothing found 😕", reply_markup=MENU)
            return
        await send_meal(update.message, context, meal, update.effective_user.id)

    await safe_run(update, context, _do())


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


async def cuisines_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]

    async def _do():
        areas = await api.list_areas()
        page = 0
        page_items, total = paginate(areas, page, PAGE_SIZE)
        await update.message.reply_text(
            "🌍 Choose a cuisine (area):",
            reply_markup=list_kb(page_items, "area", page, total)
        )

    await safe_run(update, context, _do())


async def categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]

    async def _do():
        cats = await api.list_categories()
        page = 0
        page_items, total = paginate(cats, page, PAGE_SIZE)
        await update.message.reply_text(
            "🏷️ Choose a category:",
            reply_markup=list_kb(page_items, "cat", page, total)
        )

    await safe_run(update, context, _do())


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
    if text == BTN_SETTINGS:
        await settings_cmd(update, context)
        return
    if text == BTN_HISTORY:
        await history_cmd(update, context)
        return
    if text == BTN_FAVS:
        await favorites_cmd(update, context)
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
    if text == BTN_AREA:
        await cuisines_cmd(update, context)
        return
    if text == BTN_CAT:
        await categories_cmd(update, context)
        return

    mode = context.user_data.get("mode")
    limit = clamp(db.get_max(update.effective_user.id), 1, 10)

    if mode == "set_max":
        try:
            val = int(text)
            if not (1 <= val <= 10):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a number 1–10:", reply_markup=BACK)
            return
        db.set_max(update.effective_user.id, val)
        context.user_data.pop("mode", None)
        await update.message.reply_text(f"Saved ✅ max_results={val}", reply_markup=MENU)
        return

    if mode == "name":
        async def _do():
            meals = await api.search_name(text)
            context.user_data.pop("mode", None)
            if not meals:
                await update.message.reply_text("No results 😕", reply_markup=MENU)
                return
            kb = [[InlineKeyboardButton(m.get("strMeal", "—"), callback_data=f"meal:{m.get('idMeal', '')}")]
                  for m in meals[:limit]]
            kb.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
            await update.message.reply_text("Choose a recipe:", reply_markup=InlineKeyboardMarkup(kb))

        await safe_run(update, context, _do())
        return

    if mode == "ing":
        async def _do():
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

            kb = [[InlineKeyboardButton(name_by.get(mid, "—"), callback_data=f"meal:{mid}")]
                  for mid in list(common)[:limit]]
            kb.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
            await update.message.reply_text("Choose a recipe:", reply_markup=InlineKeyboardMarkup(kb))

        await safe_run(update, context, _do())
        return

    await update.message.reply_text("Use the menu buttons 🙂", reply_markup=MENU)


async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]
    db: DB = context.application.bot_data["db"]
    query = update.callback_query
    data = query.data

    await query.answer()

    async def _do():
        if data == "menu":
            await query.message.reply_text("Menu:", reply_markup=MENU)
            return

        if data.startswith("meal:"):
            mid = data.split(":", 1)[1]
            meal = await api.lookup(mid)
            if not meal:
                await query.message.reply_text("Failed to load 😕", reply_markup=MENU)
                return
            await send_meal(query.message, context, meal, query.from_user.id)
            return

        if data.startswith("fav:"):
            mid = data.split(":", 1)[1]
            if not mid:
                return
            if db.is_fav(query.from_user.id, mid):
                db.del_fav(query.from_user.id, mid)
            else:
                meal = await api.lookup(mid)
                title = str(meal.get("strMeal") or "—") if meal else "—"
                db.add_fav(query.from_user.id, mid, title)

            try:
                await query.message.edit_reply_markup(
                    reply_markup=fav_kb(db, query.from_user.id, mid)
                )
            except:
                pass
            return

        if data.startswith("unfav:"):
            mid = data.split(":", 1)[1]
            if not mid:
                return
            db.del_fav(query.from_user.id, mid)

            limit = clamp(db.get_max(query.from_user.id), 1, 10)
            items = db.get_favs(query.from_user.id, limit)

            if not items:
                await query.message.reply_text("Favorites is empty 🙂", reply_markup=MENU)
                return

            try:
                await query.message.edit_text(
                    "⭐ Favorites:",
                    reply_markup=fav_list_kb(items)
                )
            except:
                await query.message.reply_text(
                    "⭐ Favorites:",
                    reply_markup=fav_list_kb(items)
                )
            return

        if data.startswith("confirm:"):
            _, kind, ans = data.split(":", 2)
            if ans == "no":
                await query.message.reply_text("Canceled 👍", reply_markup=MENU)
                return
            if kind == "history":
                db.clear_history(query.from_user.id)
                await query.message.reply_text("History cleared ✅", reply_markup=MENU)
                return
            if kind == "favorites":
                db.clear_favs(query.from_user.id)
                await query.message.reply_text("Favorites cleared ✅", reply_markup=MENU)
                return

        # Pagination for areas/categories
        if data.startswith("area:page:"):
            page = int(data.split(":")[-1])
            areas = await api.list_areas()
            page_items, total = paginate(areas, page, PAGE_SIZE)
            await query.message.edit_text(
                "🌍 Choose a cuisine (area):",
                reply_markup=list_kb(page_items, "area", page, total)
            )
            return

        if data.startswith("cat:page:"):
            page = int(data.split(":")[-1])
            cats = await api.list_categories()
            page_items, total = paginate(cats, page, PAGE_SIZE)
            await query.message.edit_text(
                "🏷️ Choose a category:",
                reply_markup=list_kb(page_items, "cat", page, total)
            )
            return

        # Select area/category
        if data.startswith("area:sel:"):
            area = unquote(data.split(":", 2)[2])
            all_meals = await api.filter_area(area)
            page = 0
            page_items, total = paginate(all_meals, page, PAGE_SIZE)
            await query.message.edit_text(
                f"🌍 Cuisine: <b>{html.escape(area)}</b>\nChoose a recipe:",
                parse_mode="HTML",
                reply_markup=meals_kb(page_items, "area", area, page, total)
            )
            return

        if data.startswith("cat:sel:"):
            cat = unquote(data.split(":", 2)[2])
            all_meals = await api.filter_category(cat)
            page = 0
            page_items, total = paginate(all_meals, page, PAGE_SIZE)
            await query.message.edit_text(
                f"🏷️ Category: <b>{html.escape(cat)}</b>\nChoose a recipe:",
                parse_mode="HTML",
                reply_markup=meals_kb(page_items, "cat", cat, page, total)
            )
            return

        # Pagination for meals in area/category
        if data.startswith("area_meals:page:"):
            _, _, area_q, page_s = data.split(":", 3)
            area = unquote(area_q)
            page = int(page_s)
            all_meals = await api.filter_area(area)
            page_items, total = paginate(all_meals, page, PAGE_SIZE)
            await query.message.edit_text(
                f"🌍 Cuisine: <b>{html.escape(area)}</b>\nChoose a recipe:",
                parse_mode="HTML",
                reply_markup=meals_kb(page_items, "area", area, page, total)
            )
            return

        if data.startswith("cat_meals:page:"):
            _, _, cat_q, page_s = data.split(":", 3)
            cat = unquote(cat_q)
            page = int(page_s)
            all_meals = await api.filter_category(cat)
            page_items, total = paginate(all_meals, page, PAGE_SIZE)
            await query.message.edit_text(
                f"🏷️ Category: <b>{html.escape(cat)}</b>\nChoose a recipe:",
                parse_mode="HTML",
                reply_markup=meals_kb(page_items, "cat", cat, page, total)
            )
            return

        await query.message.reply_text("Use the menu 🙂", reply_markup=MENU)

    await safe_run(update, context, _do())


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
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("favorites", favorites_cmd))
    app.add_handler(CommandHandler("clearhistory", clearhistory_cmd))
    app.add_handler(CommandHandler("clearfavorites", clearfavorites_cmd))
    app.add_handler(CommandHandler("random", random_cmd))
    app.add_handler(CommandHandler("name", name_cmd))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("cuisines", cuisines_cmd))
    app.add_handler(CommandHandler("categories", categories_cmd))

    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()


if __name__ == "__main__":
    main()