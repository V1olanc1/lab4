import os
import time
import html
import sqlite3
import logging
import traceback
from typing import Optional, List, Tuple, Dict, TypeVar
from urllib.parse import quote, unquote

import httpx
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("recipe_bot")

# ---------- UI ----------
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

PAGE_SIZE = 20  # for areas/categories/meals lists


class UserError(Exception):
    """Expected errors shown to user nicely."""
    pass


# ---------- DB ----------
class DB:
    def __init__(self, path: str = "bot.db"):
        self.path = path

    def c(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _cols(con: sqlite3.Connection, table: str) -> set:
        try:
            rows = con.execute(f"PRAGMA table_info({table})").fetchall()
            return {r[1] for r in rows}
        except sqlite3.Error:
            return set()

    def init(self):
        with self.c() as con:
            # settings
            con.execute(
                "CREATE TABLE IF NOT EXISTS settings(user_id INTEGER PRIMARY KEY, max_results INTEGER NOT NULL DEFAULT 5)"
            )

            # migrate if db is from Spoonacular (recipe_id/title)
            hist_cols = self._cols(con, "history")
            fav_cols = self._cols(con, "favorites")
            spoonacular_history = ("recipe_id" in hist_cols) and ("meal_id" not in hist_cols)
            spoonacular_favs = ("recipe_id" in fav_cols) and ("meal_id" not in fav_cols)
            if spoonacular_history:
                con.execute("DROP TABLE IF EXISTS history")
            if spoonacular_favs:
                con.execute("DROP TABLE IF EXISTS favorites")

            # TheMealDB schema
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

    def get_max(self, user_id: int) -> int:
        with self.c() as con:
            row = con.execute("SELECT max_results FROM settings WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                con.execute("INSERT INTO settings(user_id,max_results) VALUES(?,5)", (user_id,))
                return 5
            return int(row[0])

    def set_max(self, user_id: int, val: int):
        with self.c() as con:
            con.execute(
                "INSERT INTO settings(user_id,max_results) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET max_results=excluded.max_results",
                (user_id, val),
            )

    def add_history(self, user_id: int, meal_id: str, meal_name: str):
        with self.c() as con:
            con.execute(
                "INSERT INTO history(user_id,ts,meal_id,meal_name) VALUES(?,?,?,?)",
                (user_id, int(time.time()), meal_id, meal_name),
            )
            con.execute(
                "DELETE FROM history WHERE user_id=? AND id NOT IN (SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT 200)",
                (user_id, user_id),
            )

    def get_history(self, user_id: int, limit: int):
        with self.c() as con:
            rows = con.execute(
                "SELECT meal_id, meal_name, ts FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [(str(a), str(b), int(c)) for a, b, c in rows]

    def clear_history(self, user_id: int):
        with self.c() as con:
            con.execute("DELETE FROM history WHERE user_id=?", (user_id,))

    def is_fav(self, user_id: int, meal_id: str) -> bool:
        with self.c() as con:
            row = con.execute("SELECT 1 FROM favorites WHERE user_id=? AND meal_id=?", (user_id, meal_id)).fetchone()
        return bool(row)

    def add_fav(self, user_id: int, meal_id: str, meal_name: str):
        with self.c() as con:
            con.execute(
                "INSERT OR REPLACE INTO favorites(user_id, meal_id, meal_name, ts) VALUES(?,?,?,?)",
                (user_id, meal_id, meal_name, int(time.time())),
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
                "SELECT meal_id, meal_name, ts FROM favorites WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [(str(a), str(b), int(c)) for a, b, c in rows]


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


# ---------- helpers ----------
def clamp(n: int, lo: int, hi: int) -> int:
    return lo if n < lo else hi if n > hi else n


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def parse_ingredients(s: str) -> List[str]:
    s = (s or "").replace(";", ",").replace("\n", ",")
    items = [x.strip() for x in s.split(",") if x.strip()]
    return [x.lower().replace(" ", "_") for x in items][:8]


def ingredients_text(meal: dict) -> str:
    lines = []
    for i in range(1, 21):
        ing = (meal.get(f"strIngredient{i}") or "").strip()
        meas = (meal.get(f"strMeasure{i}") or "").strip()
        if ing:
            lines.append(f"• {ing}" + (f" — {meas}" if meas else ""))
    return "\n".join(lines) if lines else "—"


def meal_caption(meal: dict) -> str:
    name = (meal.get("strMeal") or "Untitled").strip()
    cat = (meal.get("strCategory") or "—").strip()
    area = (meal.get("strArea") or "—").strip()
    return trunc(f"🍽️ <b>{html.escape(name)}</b>\n🏷️ {html.escape(cat)} • {html.escape(area)}", 950)


def meal_full_text(meal: dict) -> str:
    name = (meal.get("strMeal") or "Untitled").strip()
    cat = (meal.get("strCategory") or "—").strip()
    area = (meal.get("strArea") or "—").strip()
    instr = (meal.get("strInstructions") or "No instructions provided.").strip()
    ings = ingredients_text(meal)

    body = (
        f"🍽️ <b>{html.escape(name)}</b>\n"
        f"🏷️ {html.escape(cat)} • {html.escape(area)}\n\n"
        f"<b>Ingredients:</b>\n{html.escape(ings)}\n\n"
        f"<b>Instructions:</b>\n{html.escape(instr)}"
    )
    return trunc(body, 3800)


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


T = TypeVar("T")


def paginate(items: List[T], page: int, page_size: int) -> Tuple[List[T], int]:
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


# ---------- chat cleanup ----------
MAX_TRACKED_BOT_MSGS = 30


async def _safe_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Best-effort message deletion (ignore failures / missing rights)."""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


async def _delete_user_message_from_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the user's trigger message (only for message updates, not callbacks)."""
    msg = update.message
    if not msg or not msg.from_user or getattr(msg.from_user, "is_bot", False):
        return
    await _safe_delete(context, msg.chat.id, msg.message_id)


async def _ui_cleanup(context: ContextTypes.DEFAULT_TYPE, chat_id: int, keep_ids: List[int]):
    old_ids: List[int] = context.user_data.get("bot_msg_ids", []) or []
    keep = set(keep_ids or [])
    for mid in old_ids:
        if mid in keep:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except TelegramError:
            pass
    context.user_data["bot_msg_ids"] = list(keep_ids or [])[-MAX_TRACKED_BOT_MSGS:]


async def ui_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, msg_text: str, **kwargs):
    """Send a new bot message and delete previous bot messages (keep only this one)."""
    kwargs.setdefault("quote", False)
    m = await update.effective_message.reply_text(msg_text, **kwargs)
    await _ui_cleanup(context, update.effective_chat.id, [m.message_id])
    await _delete_user_message_from_update(update, context)
    return m


async def send_meal(msg, context: ContextTypes.DEFAULT_TYPE, meal: dict, user_id: int):
    db: DB = context.application.bot_data["db"]

    meal_id = str(meal.get("idMeal") or "")
    meal_name = str(meal.get("strMeal") or "—")

    if meal_id:
        db.add_history(user_id, meal_id, meal_name)

    photo = (meal.get("strMealThumb") or "").strip()
    cap = meal_caption(meal)
    full_text = meal_full_text(meal)
    kb = fav_kb(db, user_id, meal_id) if meal_id else None

    keep_ids: List[int] = []

    if photo:
        try:
            pm = await msg.reply_photo(photo=photo, caption=cap, parse_mode="HTML", quote=False)
            keep_ids.append(pm.message_id)
        except TelegramError:
            pass

    tm = await msg.reply_text(full_text, parse_mode="HTML", reply_markup=kb, quote=False)
    keep_ids.append(tm.message_id)

    await _ui_cleanup(context, msg.chat.id, keep_ids)
    if msg.from_user and not getattr(msg.from_user, 'is_bot', False):
        await _safe_delete(context, msg.chat.id, msg.message_id)


async def safe_run(update: Update, context: ContextTypes.DEFAULT_TYPE, coro) -> None:
    try:
        await coro
        return
    except UserError as e:
        if update.effective_message:
            await ui_reply(update, context, str(e), reply_markup=MENU)
        return
    except Exception as e:
        log.error("Unexpected error: %s", e)
        log.debug("Traceback:\n%s", traceback.format_exc())
        if update.effective_message:
            await ui_reply(update, context, "⚠️ Oops, something went wrong. Please try again.", reply_markup=MENU)
        return


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mode", None)
    await ui_reply(update, context, "Hi! I'm a recipes bot 🍽️\nChoose an action:", reply_markup=MENU)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ui_reply(update, context,
                   "Commands:\n/start - Start\n/help - Help\n/random - Random recipe\n"
                   "/name - Search by name\n/find - Search by ingredients\n"
                   "/cuisines - Browse cuisines\n/categories - Browse categories\n"
                   "/history - History\n/favorites - Favorites\n"
                   "/clearhistory - Clear history\n/clearfavorites - Clear favorites\n"
                   "/settings - Settings",
                   reply_markup=MENU,
                   )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: DB = context.application.bot_data["db"]
    m = db.get_max(update.effective_user.id)
    context.user_data["mode"] = "set_max"
    await ui_reply(update, context, f"⚙️ Current max results = {m}\nSend a number 1–10:", reply_markup=BACK)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: DB = context.application.bot_data["db"]
    limit = clamp(db.get_max(update.effective_user.id), 1, 10)
    items = db.get_history(update.effective_user.id, limit)

    if not items:
        await ui_reply(update, context, "History is empty 🙂", reply_markup=MENU)
        return

    kb = [[InlineKeyboardButton(name, callback_data=f"meal:{mid}")] for mid, name, _ in items]
    kb.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])

    await ui_reply(update, context, "🕘 Recent views:", reply_markup=InlineKeyboardMarkup(kb))


async def favorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: DB = context.application.bot_data["db"]
    limit = clamp(db.get_max(update.effective_user.id), 1, 10)
    items = db.get_favs(update.effective_user.id, limit)

    if not items:
        await ui_reply(update, context, "Favorites is empty 🙂", reply_markup=MENU)
        return

    await ui_reply(update, context, "⭐ Favorites:", reply_markup=fav_list_kb(items))


async def clearhistory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ui_reply(update, context, "Clear history?", reply_markup=confirm_kb("history"))


async def clearfavorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ui_reply(update, context, "Clear favorites?", reply_markup=confirm_kb("favorites"))


async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]

    async def _do():
        meal = await api.random()
        if not meal:
            await ui_reply(update, context, "Nothing found 😕", reply_markup=MENU)
            return
        await send_meal(update.message, context, meal, update.effective_user.id)

    await safe_run(update, context, _do())


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "name"
    await ui_reply(update, context, "Send a recipe name (English):", reply_markup=BACK)


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "ing"
    await ui_reply(update, context, "Send ingredients separated by commas (English):", reply_markup=BACK)


async def cuisines_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]

    async def _do():
        areas = await api.list_areas()
        page = 0
        page_items, total = paginate(areas, page, PAGE_SIZE)
        await ui_reply(update, context,
                       "🌍 Choose a cuisine (area):",
                       reply_markup=list_kb(page_items, "area", page, total),
                       )

    await safe_run(update, context, _do())


async def categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]

    async def _do():
        cats = await api.list_categories()
        page = 0
        page_items, total = paginate(cats, page, PAGE_SIZE)
        await ui_reply(update, context,
                       "🏷️ Choose a category:",
                       reply_markup=list_kb(page_items, "cat", page, total),
                       )

    await safe_run(update, context, _do())


# ---------- text handler ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]
    db: DB = context.application.bot_data["db"]

    text = (update.message.text or "").strip()

    if text == BTN_BACK:
        context.user_data.pop("mode", None)
        await ui_reply(update, context, "OK.", reply_markup=MENU)
        return

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
            await ui_reply(update, context, "Please send a number 1–10:", reply_markup=BACK)
            return
        db.set_max(update.effective_user.id, val)
        context.user_data.pop("mode", None)
        await ui_reply(update, context, f"Saved ✅ max_results={val}", reply_markup=MENU)
        return

    if mode == "name":
        async def _do():
            meals = await api.search_name(text)
            context.user_data.pop("mode", None)
            if not meals:
                await ui_reply(update, context, "No results 😕", reply_markup=MENU)
                return
            kb = [[InlineKeyboardButton(m.get("strMeal", "—"), callback_data=f"meal:{m.get('idMeal', '')}")]
                  for m in meals[:limit]]
            kb.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
            await ui_reply(update, context, "Choose a recipe:", reply_markup=InlineKeyboardMarkup(kb))

        await safe_run(update, context, _do())
        return

    if mode == "ing":
        async def _do():
            ings = parse_ingredients(text)
            if not ings:
                await ui_reply(update, context, "Example: chicken, garlic", reply_markup=BACK)
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
                await ui_reply(update, context, "No matches 😕", reply_markup=MENU)
                return

            kb = [[InlineKeyboardButton(name_by.get(mid, "—"), callback_data=f"meal:{mid}")]
                  for mid in list(common)[:limit]]
            kb.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
            await ui_reply(update, context, "Choose a recipe:", reply_markup=InlineKeyboardMarkup(kb))

        await safe_run(update, context, _do())
        return

    await ui_reply(update, context, "Use the menu buttons 🙂", reply_markup=MENU)


# ---------- callback handler ----------
async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api: MealDB = context.application.bot_data["api"]
    db: DB = context.application.bot_data["db"]
    query = update.callback_query
    data = query.data

    await query.answer()

    async def _do():
        if data == "menu":
            await ui_reply(update, context, "Menu:", reply_markup=MENU)
            return

        if data.startswith("meal:"):
            mid = data.split(":", 1)[1]
            meal = await api.lookup(mid)
            if not meal:
                await ui_reply(update, context, "Failed to load 😕", reply_markup=MENU)
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
            except TelegramError:
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
                await ui_reply(update, context, "Favorites is empty 🙂", reply_markup=MENU)
                return

            try:
                await query.message.edit_text("⭐ Favorites:", reply_markup=fav_list_kb(items))
            except TelegramError:
                await ui_reply(update, context, "⭐ Favorites:", reply_markup=fav_list_kb(items))
            return

        if data.startswith("confirm:"):
            _, kind, ans = data.split(":", 2)
            if ans == "no":
                await ui_reply(update, context, "Canceled 👍", reply_markup=MENU)
                return
            if kind == "history":
                db.clear_history(query.from_user.id)
                await ui_reply(update, context, "History cleared ✅", reply_markup=MENU)
                return
            if kind == "favorites":
                db.clear_favs(query.from_user.id)
                await ui_reply(update, context, "Favorites cleared ✅", reply_markup=MENU)
                return

        if data.startswith("area:page:"):
            page = int(data.split(":")[-1])
            areas = await api.list_areas()
            page_items, total = paginate(areas, page, PAGE_SIZE)
            await ui_reply(update, context, "🌍 Choose a cuisine (area):",
                           reply_markup=list_kb(page_items, "area", page, total))
            return

        if data.startswith("cat:page:"):
            page = int(data.split(":")[-1])
            cats = await api.list_categories()
            page_items, total = paginate(cats, page, PAGE_SIZE)
            await ui_reply(update, context, "🏷️ Choose a category:",
                           reply_markup=list_kb(page_items, "cat", page, total))
            return

        if data.startswith("area:sel:"):
            area = unquote(data.split(":", 2)[2])
            all_meals = await api.filter_area(area)
            page = 0
            page_items, total = paginate(all_meals, page, PAGE_SIZE)
            await ui_reply(
                update,
                context,
                f"🌍 Cuisine: <b>{html.escape(area)}</b>\nChoose a recipe:",
                parse_mode="HTML",
                reply_markup=meals_kb(page_items, "area", area, page, total),
            )
            return

        if data.startswith("cat:sel:"):
            cat = unquote(data.split(":", 2)[2])
            all_meals = await api.filter_category(cat)
            page = 0
            page_items, total = paginate(all_meals, page, PAGE_SIZE)
            await ui_reply(
                update,
                context,
                f"🏷️ Category: <b>{html.escape(cat)}</b>\nChoose a recipe:",
                parse_mode="HTML",
                reply_markup=meals_kb(page_items, "cat", cat, page, total),
            )
            return

        if data.startswith("area_meals:page:"):
            _, _, area_q, page_s = data.split(":", 3)
            area = unquote(area_q)
            page = int(page_s)
            all_meals = await api.filter_area(area)
            page_items, total = paginate(all_meals, page, PAGE_SIZE)
            await ui_reply(
                update,
                context,
                f"🌍 Cuisine: <b>{html.escape(area)}</b>\nChoose a recipe:",
                parse_mode="HTML",
                reply_markup=meals_kb(page_items, "area", area, page, total),
            )
            return

        if data.startswith("cat_meals:page:"):
            _, _, cat_q, page_s = data.split(":", 3)
            cat = unquote(cat_q)
            page = int(page_s)
            all_meals = await api.filter_category(cat)
            page_items, total = paginate(all_meals, page, PAGE_SIZE)
            await ui_reply(
                update,
                context,
                f"🏷️ Category: <b>{html.escape(cat)}</b>\nChoose a recipe:",
                parse_mode="HTML",
                reply_markup=meals_kb(page_items, "cat", cat, page, total),
            )
            return

        await ui_reply(update, context, "Use the menu 🙂", reply_markup=MENU)

    await safe_run(update, context, _do())


# ---------- main ----------
def main():
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing TELEGRAM_TOKEN in .env")

    api_key = os.getenv("MEALDB_API_KEY", "1").strip() or "1"
    db_path = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"

    app = Application.builder().token(token).build()
    app.bot_data["api"] = MealDB(api_key)
    app.bot_data["db"] = DB(db_path)
    app.bot_data["db"].init()

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