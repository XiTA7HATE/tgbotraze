import os
import logging
from datetime import date
import google.generativeai as genai
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from PIL import Image
import io
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3.5-flash")

SETUP_WEIGHT, SETUP_HEIGHT, SETUP_AGE, SETUP_GENDER, SETUP_ACTIVITY, SETUP_GOAL, MAIN_MENU = range(7)

ACTIVITY_LEVELS = {
    "🛋 Минимальная": 1.2,
    "🚶 Лёгкая": 1.375,
    "🏃 Средняя": 1.55,
    "💪 Высокая": 1.725,
    "🏋️ Очень высокая": 1.9,
}

GOALS = {
    "📉 Похудеть": 0.85,
    "⚖️ Держать вес": 1.0,
    "📈 Набрать массу": 1.15,
}

ANALYZE_PROMPT = """Ты эксперт по питанию. Посмотри на фото еды и определи КБЖУ.

Ответь СТРОГО в таком формате, каждый параметр на отдельной строке:
БЛЮДО: название блюда
ПОРЦИЯ: число (только цифры)
КАЛОРИИ: число (только цифры)
БЕЛКИ: число (только цифры)
ЖИРЫ: число (только цифры)
УГЛЕВОДЫ: число (только цифры)
КОММЕНТАРИЙ: короткий совет

Не добавляй ничего лишнего. Если на фото не еда — напиши только: НЕ_ЕДА"""

TEXT_PROMPT = """Ты эксперт по питанию. Пользователь написал что съел: "{food}"

Ответь ТОЛЬКО в таком формате (без лишних слов):
БЛЮДО: [название]
ПОРЦИЯ: [число]
КАЛОРИИ: [число]
БЕЛКИ: [число]
ЖИРЫ: [число]
УГЛЕВОДЫ: [число]
КОММЕНТАРИЙ: [короткий совет]

Если это не еда — напиши: НЕ_ЕДА"""


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                weight REAL,
                height REAL,
                age INTEGER,
                gender TEXT,
                activity REAL,
                goal REAL,
                calories_goal INTEGER,
                protein_goal INTEGER,
                fat_goal INTEGER,
                carb_goal INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS food_log (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                meal_name TEXT,
                portion INTEGER,
                calories INTEGER,
                protein REAL,
                fat REAL,
                carbs REAL,
                logged_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS water_log (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount INTEGER,
                logged_at TIMESTAMP DEFAULT NOW()
            )
        """)
    logger.info("DB initialized")


def calculate_goals(weight, height, age, gender, activity, goal):
    if gender == "male":
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161
    tdee = bmr * activity
    calories = int(tdee * goal)
    protein = int(weight * 2.0)
    fat = int(calories * 0.25 / 9)
    carbs = int((calories - protein * 4 - fat * 9) / 4)
    return calories, protein, fat, carbs


def parse_nutrition(text):
    result = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def get_user(user_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = %s", (user_id,)).fetchone()


def get_today_totals(user_id):
    today = date.today()
    with get_db() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(calories),0) as cal, COALESCE(SUM(protein),0) as prot,
                   COALESCE(SUM(fat),0) as fat, COALESCE(SUM(carbs),0) as carbs
            FROM food_log WHERE user_id = %s AND DATE(logged_at) = %s
        """, (user_id, today)).fetchone()
        water = conn.execute("""
            SELECT COALESCE(SUM(amount),0) as w FROM water_log
            WHERE user_id = %s AND DATE(logged_at) = %s
        """, (user_id, today)).fetchone()
    return {
        "calories": int(row["cal"]),
        "protein": round(float(row["prot"]), 1),
        "fat": round(float(row["fat"]), 1),
        "carbs": round(float(row["carbs"]), 1),
        "water": int(water["w"])
    }


def bar(current, total, length=10):
    if total == 0:
        return "░" * length
    return "█" * int(min(current / total, 1) * length) + "░" * (length - int(min(current / total, 1) * length))


MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📊 Статистика за сегодня")],
    [KeyboardButton("💧 Добавить воду"), KeyboardButton("📅 История")],
    [KeyboardButton("🎯 Моя норма"), KeyboardButton("⚙️ Изменить профиль")],
], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if get_user(user_id):
        await update.message.reply_text("👋 С возвращением! Отправь фото еды или напиши что съел.", reply_markup=MAIN_KB)
        return MAIN_MENU
    await update.message.reply_text(
        "👋 Привет! Я твой персональный трекер питания.\n\nДавай настроим профиль!\n\n⚖️ Введи свой вес (в кг):",
        reply_markup=ReplyKeyboardRemove()
    )
    return SETUP_WEIGHT


async def setup_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        w = float(update.message.text.replace(",", "."))
        if not 30 <= w <= 300: raise ValueError
        context.user_data["weight"] = w
        await update.message.reply_text("📏 Введи свой рост (в см):")
        return SETUP_HEIGHT
    except ValueError:
        await update.message.reply_text("❌ Введи корректный вес, например: 70")
        return SETUP_WEIGHT


async def setup_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        h = float(update.message.text.replace(",", "."))
        if not 100 <= h <= 250: raise ValueError
        context.user_data["height"] = h
        await update.message.reply_text("🎂 Введи свой возраст:")
        return SETUP_AGE
    except ValueError:
        await update.message.reply_text("❌ Введи корректный рост, например: 175")
        return SETUP_HEIGHT


async def setup_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        a = int(update.message.text)
        if not 10 <= a <= 120: raise ValueError
        context.user_data["age"] = a
        kb = [[KeyboardButton("👨 Мужской"), KeyboardButton("👩 Женский")]]
        await update.message.reply_text("⚧ Укажи пол:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return SETUP_GENDER
    except ValueError:
        await update.message.reply_text("❌ Введи корректный возраст, например: 25")
        return SETUP_AGE


async def setup_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    if "Мужской" in t: context.user_data["gender"] = "male"
    elif "Женский" in t: context.user_data["gender"] = "female"
    else:
        await update.message.reply_text("❌ Выбери из кнопок")
        return SETUP_GENDER
    kb = [[KeyboardButton(k)] for k in ACTIVITY_LEVELS]
    await update.message.reply_text(
        "🏃 Уровень активности:\n\n🛋 Минимальная — сидячий образ жизни\n🚶 Лёгкая — 1-3 тренировки\n🏃 Средняя — 3-5 тренировок\n💪 Высокая — 6-7 тренировок\n🏋️ Очень высокая — физический труд",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )
    return SETUP_ACTIVITY


async def setup_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    if t not in ACTIVITY_LEVELS:
        await update.message.reply_text("❌ Выбери из кнопок")
        return SETUP_ACTIVITY
    context.user_data["activity"] = ACTIVITY_LEVELS[t]
    kb = [[KeyboardButton(k)] for k in GOALS]
    await update.message.reply_text("🎯 Какая цель?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return SETUP_GOAL


async def setup_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    if t not in GOALS:
        await update.message.reply_text("❌ Выбери из кнопок")
        return SETUP_GOAL
    context.user_data["goal"] = GOALS[t]
    d = context.user_data
    cal, prot, fat, carbs = calculate_goals(d["weight"], d["height"], d["age"], d["gender"], d["activity"], d["goal"])
    user_id = update.effective_user.id
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, weight, height, age, gender, activity, goal,
                               calories_goal, protein_goal, fat_goal, carb_goal)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET
                weight=EXCLUDED.weight, height=EXCLUDED.height, age=EXCLUDED.age,
                gender=EXCLUDED.gender, activity=EXCLUDED.activity, goal=EXCLUDED.goal,
                calories_goal=EXCLUDED.calories_goal, protein_goal=EXCLUDED.protein_goal,
                fat_goal=EXCLUDED.fat_goal, carb_goal=EXCLUDED.carb_goal
        """, (user_id, update.effective_user.username, d["weight"], d["height"], d["age"],
              d["gender"], d["activity"], d["goal"], cal, prot, fat, carbs))
    await update.message.reply_text(
        f"✅ Профиль создан!\n\n🎯 Дневная норма:\n🔥 {cal} ккал\n🥩 {prot} г белков\n🧈 {fat} г жиров\n🍞 {carbs} г углеводов\n\nОтправляй фото еды или пиши что съел!",
        reply_markup=MAIN_KB
    )
    return MAIN_MENU


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Сначала настрой профиль — напиши /start")
        return MAIN_MENU
    msg = await update.message.reply_text("🔍 Анализирую фото...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        image = Image.open(io.BytesIO(file_bytes))
        response = model.generate_content([ANALYZE_PROMPT, image])
        resp_text = response.text.strip()
        if "НЕ_ЕДА" in resp_text:
            await msg.edit_text("🤔 Не вижу еду на фото. Попробуй другое!")
            return MAIN_MENU
        await save_and_reply(update, resp_text, user, msg)
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await msg.edit_text("😔 Не удалось проанализировать. Попробуй ещё раз!")
    return MAIN_MENU


async def handle_text_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    t = update.message.text

    if t == "📊 Статистика за сегодня": await show_stats(update, context); return MAIN_MENU
    if t == "💧 Добавить воду": await add_water_prompt(update, context); return MAIN_MENU
    if t == "📅 История": await show_history(update, context); return MAIN_MENU
    if t == "🎯 Моя норма": await show_goals(update, context); return MAIN_MENU
    if t == "⚙️ Изменить профиль": return await start(update, context)
    if t.startswith("💧") and "мл" in t: await handle_water_input(update, context); return MAIN_MENU

    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Сначала настрой профиль — напиши /start")
        return MAIN_MENU

    msg = await update.message.reply_text("🔍 Анализирую...")
    try:
        response = model.generate_content(TEXT_PROMPT.format(food=t))
        result = response.text.strip()
        if "НЕ_ЕДА" in result:
            await msg.edit_text("🤔 Напиши точнее, например: *200г гречки и курица*", parse_mode="Markdown")
            return MAIN_MENU
        await save_and_reply(update, result, user, msg)
    except Exception as e:
        logger.error(f"Text error: {e}")
        await msg.edit_text("😔 Что-то пошло не так. Попробуй ещё раз!")
    return MAIN_MENU


async def save_and_reply(update, text, user, msg):
    d = parse_nutrition(text)
    try:
        meal_name = d.get("БЛЮДО", "Блюдо")
        portion = int(d.get("ПОРЦИЯ", 0))
        calories = int(d.get("КАЛОРИИ", 0))
        protein = float(d.get("БЕЛКИ", 0))
        fat = float(d.get("ЖИРЫ", 0))
        carbs = float(d.get("УГЛЕВОДЫ", 0))
        comment = d.get("КОММЕНТАРИЙ", "")
    except (ValueError, KeyError):
        await msg.edit_text("😔 Не удалось разобрать ответ. Попробуй ещё раз!")
        return

    user_id = update.effective_user.id
    with get_db() as conn:
        conn.execute(
            "INSERT INTO food_log (user_id, meal_name, portion, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (user_id, meal_name, portion, calories, protein, fat, carbs)
        )

    totals = get_today_totals(user_id)
    remaining = user["calories_goal"] - totals["calories"]
    sign = "осталось" if remaining >= 0 else "превышение"

    reply = (
        f"✅ *{meal_name}* (~{portion}г)\n\n"
        f"🔥 {calories} ккал  |  🥩 {protein}г  |  🧈 {fat}г  |  🍞 {carbs}г\n\n"
        f"━━━ Сегодня ━━━\n"
        f"🔥 {bar(totals['calories'], user['calories_goal'])} {totals['calories']}/{user['calories_goal']} ккал\n"
        f"   ({abs(remaining)} ккал {sign})\n"
        f"🥩 {bar(totals['protein'], user['protein_goal'])} {totals['protein']}/{user['protein_goal']}г белков\n"
    )
    if comment:
        reply += f"\n💡 {comment}"
    await msg.edit_text(reply, parse_mode="Markdown")


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    today = date.today()
    with get_db() as conn:
        meals = conn.execute(
            "SELECT meal_name, calories, logged_at FROM food_log WHERE user_id=%s AND DATE(logged_at)=%s ORDER BY logged_at",
            (user_id, today)
        ).fetchall()
    totals = get_today_totals(user_id)

    text = f"📊 *Статистика за {today.strftime('%d.%m.%Y')}*\n\n"
    if meals:
        text += "🍽 *Приёмы пищи:*\n"
        for m in meals:
            text += f"  {m['logged_at'].strftime('%H:%M')} — {m['meal_name']} ({m['calories']} ккал)\n"
        text += "\n"

    text += (
        f"📈 *Итого:*\n"
        f"🔥 {bar(totals['calories'], user['calories_goal'])} {totals['calories']}/{user['calories_goal']} ({int(totals['calories']/user['calories_goal']*100) if user['calories_goal'] else 0}%)\n"
        f"🥩 {bar(totals['protein'], user['protein_goal'])} {totals['protein']}/{user['protein_goal']}г\n"
        f"🧈 {bar(totals['fat'], user['fat_goal'])} {totals['fat']}/{user['fat_goal']}г\n"
        f"🍞 {bar(totals['carbs'], user['carb_goal'])} {totals['carbs']}/{user['carb_goal']}г\n"
        f"💧 {totals['water']} мл воды\n\n"
    )
    remaining = user["calories_goal"] - totals["calories"]
    text += f"✅ Осталось: *{remaining} ккал*" if remaining > 0 else f"⚠️ Превышение: *{abs(remaining)} ккал*"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)


async def show_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    gender_str = "Мужской" if user["gender"] == "male" else "Женский"
    goal_str = {0.85: "Похудение", 1.0: "Поддержание веса", 1.15: "Набор массы"}.get(float(user["goal"]), "—")
    await update.message.reply_text(
        f"🎯 *Профиль и нормы*\n\n⚖️ {user['weight']} кг\n📏 {user['height']} см\n🎂 {user['age']} лет\n⚧ {gender_str}\n🏃 {goal_str}\n\n"
        f"*Норма:*\n🔥 {user['calories_goal']} ккал\n🥩 {user['protein_goal']}г\n🧈 {user['fat_goal']}г\n🍞 {user['carb_goal']}г",
        parse_mode="Markdown", reply_markup=MAIN_KB
    )


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DATE(logged_at) as day, SUM(calories) as cal FROM food_log WHERE user_id=%s GROUP BY day ORDER BY day DESC LIMIT 7",
            (user_id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("📅 История пуста!", reply_markup=MAIN_KB)
        return
    text = "📅 *История за 7 дней*\n\n"
    for r in rows:
        cal = int(r["cal"] or 0)
        text += f"`{r['day'].strftime('%d.%m')}` {bar(cal, user['calories_goal'], 8)} {cal} ккал\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KB)


async def add_water_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [KeyboardButton("💧 150 мл"), KeyboardButton("💧 200 мл"), KeyboardButton("💧 250 мл")],
        [KeyboardButton("💧 300 мл"), KeyboardButton("💧 500 мл"), KeyboardButton("💧 1000 мл")],
    ]
    await update.message.reply_text("💧 Сколько воды выпил?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))


async def handle_water_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int("".join(filter(str.isdigit, update.message.text)))
        user_id = update.effective_user.id
        with get_db() as conn:
            conn.execute("INSERT INTO water_log (user_id, amount) VALUES (%s, %s)", (user_id, amount))
        totals = get_today_totals(user_id)
        await update.message.reply_text(
            f"💧 +{amount} мл!\nСегодня: {bar(totals['water'], 2000)} {totals['water']}/2000 мл",
            reply_markup=MAIN_KB
        )
    except Exception as e:
        logger.error(f"Water error: {e}")


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SETUP_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_weight)],
            SETUP_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_height)],
            SETUP_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_age)],
            SETUP_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_gender)],
            SETUP_ACTIVITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_activity)],
            SETUP_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_goal)],
            MAIN_MENU: [
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_food),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_food))
    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
